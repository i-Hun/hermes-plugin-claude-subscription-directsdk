import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("accounts_under_test", Path(__file__).resolve().parents[1] / "claude_accounts.py")
assert spec is not None and spec.loader is not None
accounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accounts)
RESET = "2026-10-14T12:00:00Z"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # Every test is isolated, even failure tests: never touch real credentials/network.
    paths = {key: tmp_path / key for key in ("default", "ivan")}
    for path in paths.values():
        path.mkdir(mode=0o700)
    monkeypatch.setattr(accounts, "ACCOUNT_DIRS", paths)
    monkeypatch.setattr(accounts.time, "time", lambda: 1000)
    monkeypatch.setattr(accounts.http.client, "HTTPSConnection", Mock(side_effect=AssertionError("unexpected HTTP")))
    # Never spawn the real native CLI from tests; refresh tests install their own fake.
    monkeypatch.setattr(accounts.shutil, "which", lambda name, path=None: "/fake/bin/claude")
    monkeypatch.setattr(accounts.subprocess, "Popen", Mock(side_effect=AssertionError("unexpected CLI spawn")))
    return paths


def native(monkeypatch, paths, write=None):
    """Fake native refresh: optionally rewrite credentials like the real CLI would."""
    def popen(argv, **kwargs):
        if write is not None:
            credentials(paths, **write)
        return Mock(pid=4242, wait=Mock(return_value=0))
    spawn = Mock(side_effect=popen)
    monkeypatch.setattr(accounts.subprocess, "Popen", spawn)
    return spawn


def credentials(paths, account="ivan", **overrides):
    oauth = {"accessToken": account + "-fake-token", "expiresAt": 2000000,
             "refreshToken": "NEVER-USE-THIS", "identity": "private@example.test"}
    oauth.update(overrides)
    path = paths[account] / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    path.chmod(0o600)
    return path


def http(monkeypatch, payload=None, status=200):
    connection, response = Mock(), Mock()
    response.status = status
    response.read.return_value = json.dumps(payload if payload is not None else {
        key: {"utilization": 12, "resets_at": RESET} for key, _ in accounts._ROWS}).encode()
    connection.getresponse.return_value = response
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(accounts.http.client, "HTTPSConnection", constructor)
    return constructor, connection, response


def test_snapshot_strips_overrides_and_keeps_input(isolated, monkeypatch):
    dirty = dict(PATH="/bin", HOME="/fake", KEEP="yes")
    dirty.update({key: "ambient-secret" for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY_HELPER", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_HELPER", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
        "CLAUDE_CONFIG_DIR", "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", "AWS_PROFILE",
        "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET", "API_KEY_HELPER")})
    original = dict(dirty)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "global-secret")
    global_before = dict(os.environ)
    result = accounts.account_env("ivan", dirty)
    assert result == {"PATH": "/bin", "HOME": "/fake", "KEEP": "yes",
                      "CLAUDE_CONFIG_DIR": str(isolated["ivan"]),
                      "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR": str(isolated["ivan"])}
    assert dirty == original and dict(os.environ) == global_before
    assert "global-secret" not in accounts.account_env("ivan").values()
    assert "PATH" not in accounts.account_env("ivan", {})


def test_concurrent_environments_are_independent(isolated):
    env = {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "default-old-token"}
    names = ["default", "ivan"] * 100
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(lambda name: accounts.account_env(name, env), names))
    for name, output in zip(names, outputs):
        assert output["CLAUDE_CONFIG_DIR"] == str(isolated[name])
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in output
    outputs[0]["PATH"] = "changed"
    assert outputs[1]["PATH"] == env["PATH"] == "/bin"


def test_login_metadata_only_no_read_or_chmod(isolated, monkeypatch):
    path = credentials(isolated)
    before = path.stat()
    def forbidden(*args, **kwargs):
        pytest.fail("login metadata validation must not read/chmod")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "chmod", forbidden)
    assert accounts.account_env("ivan", {}, True)["CLAUDE_CONFIG_DIR"] == str(isolated["ivan"])
    assert path.stat() == before


@pytest.mark.parametrize("kind", ["missing", "symlink", "hardlink", "directory", "fifo", "file_mode", "dir_mode", "dir_symlink", "alias"])
def test_unsafe_login_fails_closed(isolated, kind):
    old = credentials(isolated, "default")
    path = isolated["ivan"] / ".credentials.json"
    if kind == "symlink":
        path.symlink_to(old)
    elif kind == "hardlink":
        os.link(old, path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "dir_symlink":
        isolated["ivan"].rmdir()
        isolated["ivan"].symlink_to(isolated["default"], target_is_directory=True)
    elif kind == "alias":
        isolated["ivan"] = isolated["default"]
    elif kind in ("file_mode", "dir_mode"):
        credentials(isolated)
        (path if kind == "file_mode" else isolated["ivan"]).chmod(0o644 if kind == "file_mode" else 0o755)
    with pytest.raises(accounts.AccountError, match="Independent Claude login unavailable") as exc:
        accounts.account_env("ivan", {}, True)
    assert str(isolated["ivan"]) not in str(exc.value)
    assert accounts.usage_summary("ivan") == accounts.UNAVAILABLE
    accounts.http.client.HTTPSConnection.assert_not_called()


@pytest.mark.parametrize("name", ["bogus-secret", "Ivan", "../default", None])
def test_unknown_account(name):
    with pytest.raises(accounts.AccountError, match="^Unknown Claude account\\.$"):
        accounts.account_env(name)
    assert accounts.usage_summary(name) == accounts.UNAVAILABLE


@pytest.mark.parametrize("account", ["default", "ivan"])
def test_usage_selected_file_only_and_exact_request(isolated, monkeypatch, account):
    credentials(isolated, "default")
    credentials(isolated, "ivan")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "wrong-ambient-token")
    constructor, connection, response = http(monkeypatch)
    if account == "default":
        read = Mock(wraps=accounts._access_token)
        monkeypatch.setattr(accounts, "_access_token", read)
        assert accounts.usage_summary(account) == accounts.UNAVAILABLE
        read.assert_not_called()
        constructor.assert_not_called()
        return
    assert accounts.usage_summary(account) == "\n".join(
        f"{label}: 12% | reset {RESET}" for _, label in accounts._ROWS)
    constructor.assert_called_once_with("api.anthropic.com", timeout=15)
    connection.request.assert_called_once_with("GET", "/api/oauth/usage", headers={
        "Authorization": "Bearer " + account + "-fake-token", "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json"})
    response.read.assert_called_once_with(accounts._LIMIT + 1)
    connection.close.assert_called_once()


@pytest.mark.parametrize("account", ["default", "ivan"])
def test_missing_selected_credentials_never_fallback(isolated, monkeypatch, account):
    credentials(isolated, "ivan" if account == "default" else "default")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "old-token")
    assert accounts.usage_summary(account) == accounts.UNAVAILABLE
    accounts.http.client.HTTPSConnection.assert_not_called()


@pytest.mark.parametrize("overrides", [{"expiresAt": "private"}, {"expiresAt": float("nan")},
    {"accessToken": ""}, {"accessToken": "secret\r\ninjection"}])
def test_bad_credentials_no_http_or_refresh(isolated, overrides):
    path = credentials(isolated, **overrides)
    before = path.read_bytes()
    assert accounts.usage_summary("ivan") == accounts.UNAVAILABLE
    assert path.read_bytes() == before
    accounts.http.client.HTTPSConnection.assert_not_called()
    accounts.subprocess.Popen.assert_not_called()


# time() is 1000 s; margin 300 s -> anything below 1_300_000 ms counts as expired.
@pytest.mark.parametrize("expires", [0, 1000000, 1299999])
def test_expired_refresh_failure_is_login_expired(isolated, monkeypatch, expires):
    path = credentials(isolated, expiresAt=expires)
    before = path.read_bytes()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-secret")
    spawn = native(monkeypatch, isolated)          # CLI runs but writes nothing new
    assert accounts.usage_summary("ivan") == accounts.LOGIN_EXPIRED
    assert path.read_bytes() == before             # plugin itself never writes credentials
    accounts.http.client.HTTPSConnection.assert_not_called()
    spawn.assert_called_once()
    argv, kwargs = spawn.call_args.args[0], spawn.call_args.kwargs
    assert argv == ["/fake/bin/claude", "mcp", "list"]
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(isolated["ivan"])
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]
    assert kwargs["stdin"] is accounts.subprocess.DEVNULL and kwargs["start_new_session"] is True
    assert "shell" not in kwargs


def test_expired_refreshed_by_native_cli_then_fetched(isolated, monkeypatch):
    credentials(isolated, expiresAt=1000000)
    spawn = native(monkeypatch, isolated, write={"accessToken": "ivan-new-token", "expiresAt": 9000000})
    constructor, connection, _ = http(monkeypatch)
    assert accounts.usage_summary("ivan").startswith("Session: 12%")
    spawn.assert_called_once()
    constructor.assert_called_once()
    assert connection.request.call_args.kwargs["headers"]["Authorization"] == "Bearer ivan-new-token"


def test_fresh_token_never_spawns_cli(isolated, monkeypatch):
    credentials(isolated)
    http(monkeypatch)
    assert accounts.usage_summary("ivan").startswith("Session: 12%")
    accounts.subprocess.Popen.assert_not_called()


def test_missing_cli_is_login_expired(isolated, monkeypatch):
    credentials(isolated, expiresAt=0)
    monkeypatch.setattr(accounts.shutil, "which", lambda name, path=None: None)
    assert accounts.usage_summary("ivan") == accounts.LOGIN_EXPIRED
    accounts.subprocess.Popen.assert_not_called()


def test_cli_timeout_kills_process_group(isolated, monkeypatch):
    credentials(isolated, expiresAt=0)
    child = Mock(pid=4242)
    child.wait.side_effect = [accounts.subprocess.TimeoutExpired("claude", 30), 0]
    monkeypatch.setattr(accounts.subprocess, "Popen", Mock(return_value=child))
    killpg = Mock()
    monkeypatch.setattr(accounts.os, "killpg", killpg)
    assert accounts.usage_summary("ivan") == accounts.LOGIN_EXPIRED
    killpg.assert_called_once_with(4242, 9)


def test_relative_cli_path_refused(isolated, monkeypatch):
    credentials(isolated, expiresAt=0)
    monkeypatch.setattr(accounts.shutil, "which", lambda name, path=None: "bin/claude")
    assert accounts.usage_summary("ivan") == accounts.LOGIN_EXPIRED
    accounts.subprocess.Popen.assert_not_called()


def test_401_refreshes_once_then_retries_with_new_token(isolated, monkeypatch):
    credentials(isolated)
    spawn = native(monkeypatch, isolated, write={"accessToken": "ivan-new-token", "expiresAt": 9000000})
    constructor, connection, response = http(monkeypatch)
    statuses = iter([401, 200])
    type(response).status = property(lambda self: next(statuses))
    assert accounts.usage_summary("ivan").startswith("Session: 12%")
    spawn.assert_called_once()
    assert constructor.call_count == 2
    assert connection.request.call_args_list[-1].kwargs["headers"]["Authorization"] == "Bearer ivan-new-token"


def test_refresh_that_blanks_token_is_login_expired(isolated, monkeypatch):
    # Real CLI with a revoked refresh token rewrites the file with an empty accessToken.
    credentials(isolated, expiresAt=0)
    native(monkeypatch, isolated, write={"accessToken": "", "expiresAt": 0})
    assert accounts.usage_summary("ivan") == accounts.LOGIN_EXPIRED


def test_severity_allowlisted_spend_dropped(isolated, monkeypatch):
    credentials(isolated)
    http(monkeypatch, {"limits": [{"kind": "session", "percent": 91, "resets_at": RESET, "severity": "warning"},
                                  {"kind": "weekly_all", "percent": 5, "resets_at": RESET, "severity": "Bearer x"}],
                       "spend": {"enabled": True, "disclaimer": "private@example.test"}})
    assert accounts.usage_summary("ivan") == (
        f"Session: 91% | reset {RESET} | severity warning\nWeekly: 5% | reset {RESET}")


def test_401_twice_is_login_expired_without_body(isolated, monkeypatch):
    credentials(isolated)
    spawn = native(monkeypatch, isolated)
    constructor, connection, response = http(monkeypatch, status=401)
    response.read.return_value = b"raw fake-token private@example.test"
    assert accounts.usage_summary("ivan") == accounts.LOGIN_EXPIRED
    spawn.assert_called_once()                     # exactly one refresh, no loop
    assert constructor.call_count == 2
    response.read.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 403, 429, 500])
def test_http_errors_no_redirect_or_body_read(isolated, monkeypatch, status, capsys, caplog):
    credentials(isolated)
    _, connection, response = http(monkeypatch, status=status)
    response.getheader.return_value = "https://evil.test/private@example.test"
    response.read.return_value = b"raw fake-token private@example.test"
    assert accounts.usage_summary("ivan") == accounts.UNAVAILABLE
    assert connection.request.call_count == 1
    response.read.assert_not_called()
    connection.close.assert_called_once()
    assert not capsys.readouterr().out and not caplog.text


@pytest.mark.parametrize("phase", ["request", "getresponse", "read", "close"])
def test_exception_sanitization(isolated, monkeypatch, phase, capsys, caplog):
    credentials(isolated)
    _, connection, response = http(monkeypatch)
    target = response if phase == "read" else connection
    getattr(target, phase).side_effect = RuntimeError("Bearer fake-secret private@example.test")
    result = accounts.usage_summary("ivan")
    assert "fake-secret" not in result and "private@" not in result
    if phase != "close":
        assert result == accounts.UNAVAILABLE
    captured = capsys.readouterr()
    assert not captured.out and not captured.err and not caplog.text


@pytest.mark.parametrize("raw", [b"not-json secret", b"[]", b"null", b"x" * (128 * 1024 + 1)])
def test_bad_response_sanitized(isolated, monkeypatch, raw):
    credentials(isolated)
    _, _, response = http(monkeypatch)
    response.read.return_value = raw
    assert accounts.usage_summary("ivan") == accounts.UNAVAILABLE


@pytest.mark.parametrize("value,reset", [(True, RESET), (101, RESET), (-1, RESET), (float("nan"), RESET),
    ("fake-secret", RESET), (12, "private@example.test"), (12, "2026-99-99T12:00:00Z"),
    (12, RESET + "\nBearer secret")])
def test_only_safe_usage_rows(isolated, monkeypatch, value, reset):
    credentials(isolated)
    http(monkeypatch, {"five_hour": {"utilization": value, "resets_at": reset},
                       "identity": "private@example.test", "secret": "fake-token"})
    assert accounts.usage_summary("ivan") == accounts.UNAVAILABLE


def test_corrupt_credentials_unavailable(isolated):
    path = credentials(isolated)
    path.write_text("raw fake-token private@example.test")
    assert accounts.usage_summary("ivan") == accounts.UNAVAILABLE
    accounts.http.client.HTTPSConnection.assert_not_called()


def test_current_limits_array_sanitized(isolated, monkeypatch):
    credentials(isolated)
    http(monkeypatch, {'limits': [
        {'kind': 'session', 'percent': 0.5, 'resets_at': RESET},
        {'kind': 'weekly_scoped', 'percent': 14, 'resets_at': RESET,
         'scope': {'model': {'display_name': 'Fable'}}},
        {'kind': 'weekly_scoped', 'percent': 99, 'resets_at': RESET,
         'scope': {'model': {'display_name': 'private@example.test'}}}]})
    assert accounts.usage_summary('ivan') == f'Session: 0.5% | reset {RESET}\nWeekly Fable: 14% | reset {RESET}'
