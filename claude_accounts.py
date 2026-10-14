"""Local account isolation and read-only OAuth usage (no token fallback).

Usage never refreshes credentials itself: when the selected token is expired (or the
API rejects it) it asks the isolated native Claude CLI to refresh under its own lock,
then re-reads that account's file once.

Paths are intentionally local deployment defaults. Importing this module performs no I/O.
Login validation checks metadata only; it never reads, creates or chmods credentials.
"""
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from datetime import datetime

ACCOUNT_DIRS = {"default": Path("/home/hun/.claude"), "ivan": Path("/home/hun/.claude-ivan")}
UNAVAILABLE = "Usage unavailable."
import logging as _logging
_log = _logging.getLogger("claude_accounts.usage")
LOGIN_EXPIRED = "Usage login expired."
# Refresh this long before expiry so a request never races the 8 h token lifetime.
_REFRESH_MARGIN_S = 300
_REFRESH_TIMEOUT_S = 30
# Cheapest native command observed to run the CLI's own OAuth refresh (no model request).
_REFRESH_ARGV = ("mcp", "list")
# Owner shutdown policy: restoring a credential file must not re-enable usage.
USAGE_ENABLED_ACCOUNTS = frozenset({"ivan"})
LOGIN_ERROR = "Independent Claude login unavailable; check account login and directory/file permissions (700/600)."
# Drop all Claude overrides, including OAuth token/FD, API-key helpers, custom
# config and backend selectors. Cloud credentials must not select another backend.
_AUTH_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "AWS_", "AMAZON_", "BEDROCK_",
                  "GOOGLE_", "GCLOUD_", "VERTEX_", "AZURE_", "CLOUD_ML_")
_AUTH_KEYS = {"API_KEY_HELPER", "CREDENTIALS_FILE", "GOOGLE_APPLICATION_CREDENTIALS", "CLAUDECODE"}
_LIMIT = 128 * 1024
_ROWS = (("five_hour", "Session"), ("seven_day", "Weekly"),
         ("seven_day_opus", "Weekly Opus"), ("seven_day_sonnet", "Weekly Sonnet"))
_SEVERITY = re.compile(r"[a-z_]{1,20}")
_RESET = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")


class _TokenExpired(Exception):
    """Selected token is (nearly) expired; only the native CLI may refresh it."""


class AccountError(RuntimeError):
    """Sanitized account failure, not a local argument-validation error.

    Hermes permits fallback for unclassified ValueError/TypeError even when the
    provider opts out. Keep this a RuntimeError so the isolated route's explicit
    non-retryable/no-fallback verdict is terminal.
    """


def _directory(account):
    if not isinstance(account, str) or account not in ACCOUNT_DIRS:
        raise AccountError("Unknown Claude account.")
    return Path(ACCOUNT_DIRS[account])


def _credentials_metadata(account):
    directory = _directory(account)
    try:
        ds = directory.lstat()
        path = directory / ".credentials.json"
        fs = path.lstat()
        if not stat.S_ISDIR(ds.st_mode) or not stat.S_ISREG(fs.st_mode) or fs.st_nlink != 1:
            raise ValueError()
        if account == "ivan":
            # Also reject an alias through a symlinked parent or a misconfigured mapping.
            if directory.resolve() == Path(ACCOUNT_DIRS["default"]).resolve():
                raise ValueError()
            if stat.S_IMODE(ds.st_mode) != 0o700 or stat.S_IMODE(fs.st_mode) != 0o600:
                raise ValueError()
            if hasattr(os, "getuid") and (ds.st_uid != os.getuid() or fs.st_uid != os.getuid()):
                raise ValueError()
        return path, fs
    except (OSError, ValueError, RuntimeError):
        raise AccountError(LOGIN_ERROR) from None


def account_env(account, env=None, require_login=False):
    """Return a fresh child environment; never modify the caller or os.environ.

    require_login verifies an independent regular credentials file, not token freshness:
    refreshing that file is exclusively the isolated native Claude child's job.
    """
    directory = _directory(account)
    if require_login:
        _credentials_metadata(account)
    child = {key: value for key, value in dict(os.environ if env is None else env).items()
             if not key.upper().startswith(_AUTH_PREFIXES) and key.upper() not in _AUTH_KEYS}
    child["CLAUDE_CONFIG_DIR"] = str(directory)
    child["CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"] = str(directory)
    return child


def _access_token(account):
    path, before = _credentials_metadata(account)
    # O_NONBLOCK avoids hanging if a file is replaced with a FIFO between stat/open.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError()
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise ValueError()
        if account == "ivan" and stat.S_IMODE(current.st_mode) != 0o600:
            raise ValueError()
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError()
    oauth = json.loads(raw)["claudeAiOauth"]
    token, expires = oauth["accessToken"], oauth["expiresAt"]
    if not isinstance(token, str) or not token or not token.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in token):
        raise ValueError()
    if type(expires) not in (int, float) or not math.isfinite(expires):
        raise ValueError()
    if expires <= (time.time() + _REFRESH_MARGIN_S) * 1000:
        raise _TokenExpired()
    return token


def _native_refresh(account):
    """Let the isolated native CLI refresh its own credentials under its own lock.

    Plugin code never reads, sends or writes the refresh token. Output is discarded;
    the only signal used is whether the credentials file afterwards holds a fresh token.
    """
    env = account_env(account, require_login=True)
    exe = shutil.which("claude", path=env.get("PATH") or os.defpath)
    if not exe:
        return False
    if not os.path.isabs(exe):
        return False
    try:
        child = subprocess.Popen([exe, *_REFRESH_ARGV], env=env, cwd=str(_directory(account)),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    ok = True
    try:
        child.wait(timeout=_REFRESH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        ok = False
    # Always reap the whole session: `mcp list` may leave MCP-server grandchildren behind.
    try:
        os.killpg(child.pid, 9)
    except OSError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return ok


def _fresh_token(account, force=False):
    """Token for this account only; one native refresh when expired or forced."""
    if not force:
        try:
            return _access_token(account)
        except _TokenExpired:
            pass
    if not _native_refresh(account):
        raise _TokenExpired()
    try:
        return _access_token(account)
    except (OSError, ValueError, KeyError, TypeError, AccountError):
        # A failed native refresh blanks the token: that is an expired login, not corruption.
        raise _TokenExpired() from None


def _format_usage(payload):
    lines = []
    rows = [(label, payload.get(key)) for key, label in _ROWS]
    # Current endpoint also serves limits[]; map known fields, never print API labels.
    kinds = {'session': 'Session', 'weekly_all': 'Weekly', 'weekly_opus': 'Weekly Opus',
             'weekly_sonnet': 'Weekly Sonnet', 'monthly': 'Monthly'}
    for item in payload.get('limits') or []:
        if not isinstance(item, dict):
            continue
        label = kinds.get(item.get('kind'))
        if item.get('kind') == 'weekly_scoped':
            scope = item.get('scope') or {}
            model = scope.get('model') if isinstance(scope, dict) else None
            name = model.get('display_name') if isinstance(model, dict) else None
            label = {'Opus': 'Weekly Opus', 'Sonnet': 'Weekly Sonnet', 'Fable': 'Weekly Fable'}.get(name)
        if label:
            rows.append((label, {'utilization': item.get('percent'), 'resets_at': item.get('resets_at'),
                                 'severity': item.get('severity')}))
    seen = set()
    for label, row in reversed(rows):
        if label in seen:
            continue
        if not isinstance(row, dict):
            continue
        value, reset = row.get("utilization"), row.get("resets_at")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 100:
            continue
        if not isinstance(reset, str) or not _RESET.fullmatch(reset):
            continue
        try:
            datetime.fromisoformat(reset.replace("Z", "+00:00"))
        except ValueError:
            continue
        severity = row.get("severity")
        tail = f" | severity {severity}" if isinstance(severity, str) and _SEVERITY.fullmatch(severity) else ""
        lines.append(f"{label}: {value:g}% | reset {reset}{tail}")
        seen.add(label)
    return "\n".join(reversed(lines)) or UNAVAILABLE


def usage_summary(account):
    """Fetch only this account's usage. No redirects, ambient auth or plugin-side refresh.

    Expired token or HTTP 401: one native CLI refresh, then one retry; still failing
    returns LOGIN_EXPIRED (re-login needed) instead of the generic UNAVAILABLE.

    HTTPSConnection verifies TLS and does not follow redirects or inherit proxy settings.
    All failures are fixed text; raw bodies, credentials and identities are never logged.
    """
    connection = None
    try:
        if account not in USAGE_ENABLED_ACCOUNTS:
            return UNAVAILABLE
        try:
            token = _fresh_token(account)
        except _TokenExpired:
            return LOGIN_EXPIRED
        for attempt in (0, 1):
            connection = http.client.HTTPSConnection("api.anthropic.com", timeout=15)
            connection.request("GET", "/api/oauth/usage", headers={
                "Authorization": "Bearer " + token,
                "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"})
            response = connection.getresponse()
            status = response.status
            if status == 401 and attempt == 0:
                connection.close()
                connection = None
                try:
                    token = _fresh_token(account, force=True)
                except _TokenExpired:
                    return LOGIN_EXPIRED
                continue
            break
        if status == 401:
            return LOGIN_EXPIRED
        if status != 200:
            _log.warning("usage[%s]: HTTP %s", account, status)
            return UNAVAILABLE
        raw = response.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            _log.warning("usage[%s]: body over limit", account)
            return UNAVAILABLE
        out = _format_usage(json.loads(raw))
        if out == UNAVAILABLE:
            _log.warning("usage[%s]: no known limit rows in payload", account)
        return out
    except Exception as exc:
        # Class name only: no message text, body, token or identity crosses this boundary.
        _log.warning("usage[%s]: %s", account, type(exc).__name__)
        return UNAVAILABLE
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
