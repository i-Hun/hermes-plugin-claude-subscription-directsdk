"""Default-profile /limits. No credential access, polling, or account guessing.

Account API: sibling claude_accounts.usage_summary(account) -> sanitized text.
Canonical accepted rows: ``Session: 12% | reset 2026-10-14T12:00:00+00:00``.
Other accepted labels are in WINDOWS; unknown/raw text is never forwarded.
Owner opt-in: config.yaml limits.owner_ids.telegram = "<numeric user id>".
An allowlist, username, home channel or group administrator is NOT an owner.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import math
import os
import re
import tempfile
import sys
from datetime import datetime
from pathlib import Path

PROVIDERS = {
    "claude-subscription-directsdk-experimental": "default",
    "claude-subscription-directsdk-ivan": "ivan",
}
LABELS = {"default": "Claude default", "ivan": "Claude Ivan"}
WINDOWS = ("Session", "Weekly", "Weekly Opus", "Weekly Sonnet", "Weekly Fable", "Monthly")
UNAVAILABLE = "Лимиты недоступны для текущего контекста провайдера."
DENIED = "Общий отчёт доступен только явно настроенному владельцу через Telegram DM."
LOGIN_EXPIRED_TEXT = "Вход истёк и сам не обновился — нужен /claude-login {account}."
DM_FAILED = "Не удалось доставить личное сообщение. Общий отчёт здесь не публикуется."
DEFAULT_HOME = Path("/home/hun/.hermes")
# Last /limits snapshot per account: window -> [pct, reset]. Numbers only, no API text.
SNAPSHOT = DEFAULT_HOME / "state" / "limits-snapshot.json"
# API reset stamps jitter by fractions of a second (10:59:59.9 vs 11:00:00.1); a real
# roll-over moves the reset by hours. Anything within this tolerance is the same window.
_ROLL_TOLERANCE_S = 600


WINDOW_TITLES = {
    "Session": "Сессия (5 ч)", "Weekly": "Неделя", "Weekly Opus": "Неделя · Opus",
    "Weekly Sonnet": "Неделя · Sonnet", "Weekly Fable": "Неделя · Fable", "Monthly": "Месяц",
}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _bar(pct):
    filled = min(10, max(0, round(pct / 10)))
    return "▰" * filled + "▱" * (10 - filled)


def _mark(pct):
    return "🔴" if pct >= 80 else "🟡" if pct >= 50 else "🟢"


def _left(delta_s):
    minutes = max(0, int(delta_s // 60))
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {mins} мин"
    return f"{mins} мин"


def _reset_text(reset, now):
    local = reset.astimezone(now.tzinfo)
    days = (local.date() - now.date()).days
    day = {0: "сегодня", 1: "завтра"}.get(days) or f"{WEEKDAYS[local.weekday()]} {local:%d.%m}"
    return f"сброс {day} в {local:%H:%M} · через {_left((reset - now).total_seconds())}"


def _delta(window, pct, reset, prev):
    """Change since the previous /limits; a rolled-over window is not a negative delta."""
    old = (prev or {}).get("windows", {}).get(window)
    if not old:
        return ""
    try:
        old_reset = datetime.fromisoformat(old[1])
        rolled = abs((reset - old_reset).total_seconds()) > _ROLL_TOLERANCE_S
    except (TypeError, ValueError):
        return ""
    if rolled:
        return " · новое окно"
    raw = pct - old[0]
    diff = int(math.copysign(math.floor(abs(raw) + 0.5), raw))  # half-up, not banker's
    return " · =" if diff == 0 else f" · {diff:+d} п.п."


def parse_rows(text):
    """Allowlisted (window, pct, reset, severity) rows; anything else is dropped."""
    rows = []
    for line in text.splitlines()[:64]:
        match = re.fullmatch(
            r"(" + "|".join(WINDOWS) + r"): (\d{1,3}(?:\.\d{1,2})?)% \| reset (\S+)"
            r"(?: \| severity ([a-z_]{1,20}))?", line
        )
        if not match or not 0 <= float(match[2]) <= 100:
            continue
        try:
            reset = datetime.fromisoformat(match[3].replace("Z", "+00:00"))
            if reset.tzinfo is None:
                continue
        except ValueError:
            continue
        rows.append((match[1], float(match[2]), reset, match[4]))
    return rows


def sanitize_summary(account, text, now=None, prev=None):
    """Reconstruct from an allowlist; never echo error bodies, identities or API fields."""
    now = now or datetime.now().astimezone()
    if not isinstance(text, str):
        return LABELS[account] + "\nНедоступно."
    rows = []
    for window, pct, reset, severity in parse_rows(text):
        flag = f"  ⚠️ {severity}" if severity and severity != "normal" else ""
        try:
            delta = _delta(window, pct, reset, prev)
        except Exception:
            delta = ""  # a bad snapshot costs only the delta, never the reply
        rows.append(f"{_mark(pct)} {WINDOW_TITLES[window]} — {pct:g}%  {_bar(pct)}{flag}"
                    f"{delta}\n     {_reset_text(reset, now)}")
    if not rows:
        return LABELS[account] + "\nНедоступно."
    tz = now.strftime("%Z") or "local"
    offset = now.strftime("%z")
    since = ""
    taken = (prev or {}).get("taken")
    try:
        if isinstance(taken, (int, float)) and not isinstance(taken, bool):
            since = f" · Δ к прошлому /limits {datetime.fromtimestamp(taken, now.tzinfo):%d.%m %H:%M}"
    except Exception:
        since = ""
    return (f"{LABELS[account]} — лимиты подписки\n\n" + "\n".join(rows)
            + f"\n\nСнято {now:%d.%m %H:%M}{since} · время — {tz} (UTC{offset[:3]}:{offset[3:]}).")


def _number(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def load_snapshot(account, path=None):
    """Shape-validated previous snapshot; anything malformed is dropped, never raised."""
    try:
        data = json.loads(Path(path or SNAPSHOT).read_text()).get(account)
        if not isinstance(data, dict) or not isinstance(data.get("windows"), dict):
            return None
        windows = {}
        for name, value in data["windows"].items():
            if (name in WINDOWS and isinstance(value, list) and len(value) == 2
                    and _number(value[0], 0, 100) and isinstance(value[1], str)):
                try:
                    if datetime.fromisoformat(value[1]).tzinfo is not None:
                        windows[name] = value
                except ValueError:
                    continue
        taken = data.get("taken")
        return {"windows": windows,
                "taken": taken if _number(taken, 0, 4102444800) else None}  # until 2100
    except Exception:
        return None


def save_snapshot(account, text, path=None, now=None):
    """Persist numbers only; failure to save never breaks the reply."""
    path = Path(path or SNAPSHOT)
    tmp = None
    try:
        rows = parse_rows(text)
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize concurrent /limits (two chats, /limits all): read-modify-write under one lock.
        lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(path.read_text())
                data = data if isinstance(data, dict) else {}
            except Exception:
                data = {}
            data[account] = {"taken": (now or datetime.now().astimezone()).timestamp(),
                             "windows": {w: [p, r.isoformat()] for w, p, r, _ in rows}}
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps(data))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            tmp = None
    except Exception:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def account_helper():
    """Explicit file loading avoids accidentally importing another profile's module."""
    name = "_default_limits_claude_accounts"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("claude_accounts.py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("Account helper unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def usage_summary(account):
    return account_helper().usage_summary(account)


def safe_usage(account, fetch):
    try:
        helper = account_helper()
        if account not in helper.USAGE_ENABLED_ACCOUNTS:
            return LABELS[account] + "\nОтключён владельцем."
        text = fetch(account)
        if not isinstance(text, str) or text == getattr(helper, "UNAVAILABLE", None):
            text = fetch(account)   # one retry: a token mid-refresh answers on the second read
        if text == helper.LOGIN_EXPIRED:
            return LABELS[account] + "\n" + LOGIN_EXPIRED_TEXT.format(account=account)
        if not isinstance(text, str) or text == getattr(helper, "UNAVAILABLE", None):
            return LABELS[account] + "\nНедоступно (повтори через минуту; причина — в errors.log)."
        try:
            out = sanitize_summary(account, text, prev=load_snapshot(account))
        except Exception:
            out = sanitize_summary(account, text)  # snapshot never costs the reply
        save_snapshot(account, text)               # and a bad snapshot heals here
        return out
    except Exception:
        return LABELS[account] + "\nНедоступно."


async def dispatch(args, *, provider, platform, user_id, owner_id, send_dm, fetch=usage_summary):
    """Pure policy boundary: owner check precedes every all-account read."""
    if args == "all":
        if type(owner_id) is int:
            owner_id = str(owner_id)
        if (platform != "telegram" or not isinstance(owner_id, str)
                or not re.fullmatch(r"[1-9][0-9]*", owner_id) or user_id != owner_id):
            return DENIED
        text = "\n\n".join([await asyncio.to_thread(safe_usage, a, fetch) for a in LABELS])
        try:
            if await send_dm(owner_id, text) is True:
                return "Общий отчёт отправлен в личные сообщения."
        except Exception:
            pass
        return DM_FAILED  # Never return `text`, even in a private invoking chat.
    if args:
        return "Использование: /limits или /limits all (владельцу в DM)."
    account = PROVIDERS.get(provider)
    if account is None:
        return UNAVAILABLE
    return await asyncio.to_thread(safe_usage, account, fetch)


def config_provider(config):
    """Profile primary route from load_config() of the default profile (no other profiles).

    Whatever load_config resolves must still match a PROVIDERS key exactly, else fail-closed.
    """
    model = config.get("model") if isinstance(config, dict) else None
    provider = model.get("provider") if isinstance(model, dict) else None
    return provider if isinstance(provider, str) else None


def session_provider(runner, key, default_provider=None):
    """Session /model override first; with none, the profile's config.yaml route.

    /model persists the choice to config.yaml and clears the session override, and /new
    starts without one, so the configured route is what the chat actually runs on.
    No env fallback and no account guessing: an unknown provider stays fail-closed.
    """
    route = runner._session_model_override(key)
    if route is None:
        route = runner.session_store.get_model_override(key)
    if route is None:
        return default_provider
    return route.get("provider") if isinstance(route, dict) else None


async def handle(args):
    try:
        from hermes_cli.config import load_config
        from hermes_constants import get_hermes_home
        from gateway import session_context as sc
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform

        if Path(get_hermes_home()).resolve() != DEFAULT_HOME:
            return UNAVAILABLE
        # Read bound ContextVars directly: get_session_env falls back to ambient env.
        def bound(name):
            value = sc._VAR_MAP["HERMES_SESSION_" + name].get()
            return value if isinstance(value, str) else ""
        if bound("PROFILE") not in ("", "default"):
            return UNAVAILABLE
        key = bound("KEY")
        runner = _gateway_runner_ref()
        if not key or runner is None:
            return UNAVAILABLE
        config = load_config()
        provider = session_provider(runner, key, config_provider(config))
        owner_id = ((config.get("limits") or {}).get("owner_ids") or {}).get("telegram")

        async def send_dm(user_id, text):
            adapter = runner._authorization_adapter(Platform.TELEGRAM, "default")
            if adapter is None:
                return False
            result = await adapter.send(chat_id=user_id, content=text, reply_to=None, metadata={})
            return getattr(result, "success", False) is True

        return await dispatch(args.strip(), provider=provider, platform=bound("PLATFORM"),
                              user_id=bound("USER_ID"), owner_id=owner_id, send_dm=send_dm)
    except Exception:
        # Gateway logs handler exceptions and could fall through to the LLM. Never raise.
        return UNAVAILABLE
