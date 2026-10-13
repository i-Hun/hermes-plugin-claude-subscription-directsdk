import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("limits_under_test", ROOT / "limits_command.py")
limits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limits)
OLD, IVAN = limits.PROVIDERS
ROW = "Session: 12% | reset 2026-10-14T12:00:00Z"


def run(args="", **kwargs):
    defaults = dict(provider=IVAN, platform="telegram", user_id="123", owner_id="123",
                    send_dm=no_dm, fetch=lambda account: ROW)
    defaults.update(kwargs)
    return asyncio.run(limits.dispatch(args, **defaults))


async def no_dm(*args):
    pytest.fail("unexpected DM")


@pytest.mark.parametrize("provider,account", [(OLD, "default"), (IVAN, "ivan")])
def test_selected_only(provider, account):
    calls = []
    text = run(provider=provider, fetch=lambda a: calls.append(a) or ROW)
    if account == "default":
        assert calls == []
        assert "Отключён владельцем" in text
        return
    assert calls == [account]
    assert text.startswith(limits.LABELS[account])
    assert "12%" in text
    assert limits.LABELS[{"default": "ivan", "ivan": "default"}[account]] not in text


@pytest.mark.parametrize("provider", [None, "", "anthropic", "openai-codex", "claude-subscription-directsdk-ivan "])
def test_unknown_provider_fail_closed(provider):
    def never(a):
        pytest.fail("unknown provider read an account")
    assert run(provider=provider, fetch=never) == limits.UNAVAILABLE


@pytest.mark.parametrize("owner,user,platform", [(None,"123","telegram"), ("","123","telegram"),
    ("@owner","@owner","telegram"), ("123","999","telegram"), ("123","123","discord"),
    ("-123","-123","telegram"), (True,"True","telegram")])
def test_owner_denial_before_read(owner,user,platform):
    def never(a):
        pytest.fail("denied owner read accounts")
    assert run("all", owner_id=owner, user_id=user, platform=platform, fetch=never) == limits.DENIED


def test_group_dm_success():
    calls, sent = [], []
    async def dm(target, text):
        sent.append((target, text))
        return True
    result = run("all", send_dm=dm, fetch=lambda a: calls.append(a) or ROW)
    assert calls == ["ivan"]
    assert sent[0][0] == "123"
    assert "Claude default" in sent[0][1] and "Claude Ivan" in sent[0][1]
    assert "Claude" not in result and "12%" not in result


def test_owner_numeric_config_from_cli():
    sent = []
    async def dm(target, text):
        sent.append(target)
        return True
    assert 'отправлен' in run('all', owner_id=123, send_dm=dm)
    assert sent == ['123']


@pytest.mark.parametrize("failure", [False, None, "true", "exception"])
def test_group_dm_failure_never_falls_back(failure):
    async def dm(target, text):
        if failure == "exception":
            raise RuntimeError("Authorization: Bearer SECRET owner@example.com")
        return failure
    result = run("all", send_dm=dm)
    assert result == limits.DM_FAILED
    assert all(s not in result for s in ("12%", "Claude", "SECRET", "owner@example.com"))


def test_sanitization_allowlist():
    raw = ROW + "\nowner@example.com\nAuthorization: Bearer sk-ant-SECRET\n" + \
        "Session: 10% | reset SECRET\nSecret: 15% | reset 2026-10-14T12:00:00Z\n" + \
        "Session: 999% | reset 2026-10-14T12:00:00Z\nSession: 11% | reset 2026-10-14\n"
    output = run(fetch=lambda a: raw)
    assert output.startswith("Claude Ivan — лимиты подписки")
    assert output.count("Сессия (5 ч)") == 1 and "12%" in output
    assert all(s not in output for s in ("SECRET", "owner@", "Bearer", "999", "15%", "10%", "11%"))


def test_readable_format_fixed_clock():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 13, 14, 0, tzinfo=timezone(timedelta(hours=2), "CEST"))
    raw = ("Session: 4% | reset 2026-10-13T16:20:00.182591+00:00\n"
           "Weekly: 55% | reset 2026-10-14T03:00:00+00:00\n"
           "Weekly Fable: 85.5% | reset 2026-10-20T03:00:00Z")
    assert limits.sanitize_summary("ivan", raw, now=now) == (
        "Claude Ivan — лимиты подписки\n\n"
        "🟢 Сессия (5 ч) — 4%  ▱▱▱▱▱▱▱▱▱▱\n     сброс сегодня в 18:20 · через 4 ч 20 мин\n"
        "🟡 Неделя — 55%  ▰▰▰▰▰▰▱▱▱▱\n     сброс завтра в 05:00 · через 15 ч 0 мин\n"
        "🔴 Неделя · Fable — 85.5%  ▰▰▰▰▰▰▰▰▰▱\n     сброс вт 20.10 в 05:00 · через 6 дн 15 ч\n\n"
        "Время — CEST (UTC+02:00).")


def test_no_valid_rows_unavailable():
    assert limits.sanitize_summary("ivan", "garbage\nSession: 5% | reset nope") == "Claude Ivan\nНедоступно."


def test_fetch_error_sanitized():
    def fail(a):
        raise RuntimeError("raw secret email@example.com")
    assert run(fetch=fail) == "Claude Ivan\nНедоступно."


@pytest.mark.parametrize("args", ["default", "ivan", "--all", "all ivan"])
def test_no_account_argument_override(args):
    def never(a):
        pytest.fail("invalid args read accounts")
    assert "Использование" in run(args, fetch=never)


def test_session_provider_memory_persisted_then_config():
    runner = SimpleNamespace(_session_model_override=lambda k: {"provider": IVAN},
        session_store=SimpleNamespace(get_model_override=lambda k: {"provider": OLD}))
    # Session override always beats the configured route.
    assert limits.session_provider(runner, "key", OLD) == IVAN
    runner._session_model_override = lambda k: None
    assert limits.session_provider(runner, "key", IVAN) == OLD
    runner.session_store.get_model_override = lambda k: None
    # No override at all (after /model persisted globally, or after /new): config route.
    assert limits.session_provider(runner, "key", IVAN) == IVAN
    assert limits.session_provider(runner, "key") is None
    runner._session_model_override = lambda k: {}
    runner.session_store.get_model_override = lambda k: {"provider": IVAN}
    assert limits.session_provider(runner, "key", IVAN) is None


@pytest.mark.parametrize("config,expected", [
    ({"model": {"provider": IVAN}}, IVAN), ({"model": {"provider": OLD}}, OLD),
    ({"model": "claude"}, None), ({"model": {"provider": 5}}, None), ({}, None), (None, None)])
def test_config_provider(config, expected):
    assert limits.config_provider(config) == expected


def _handle_without_override(monkeypatch, config, fetch_calls):
    import sys
    import hermes_constants
    import hermes_cli.config
    from gateway import session_context as sc
    runner = SimpleNamespace(_session_model_override=lambda key: None,
        session_store=SimpleNamespace(get_model_override=lambda key: None),
        _authorization_adapter=lambda platform, profile: None)
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=lambda: runner))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: limits.DEFAULT_HOME)
    monkeypatch.setattr(hermes_cli.config, "load_config", lambda: config)
    real_dispatch = limits.dispatch
    async def dispatch(args, **kw):
        return await real_dispatch(args, **kw, fetch=lambda a: fetch_calls.append(a) or ROW)
    monkeypatch.setattr(limits, "dispatch", dispatch)
    tokens = sc.set_session_vars(platform="telegram", user_id="123", chat_id="-100999", chat_type="group",
                                 thread_id="1", profile="default", session_key="fresh-after-new")
    try:
        return asyncio.run(limits.handle(""))
    finally:
        sc.clear_session_vars(tokens)


def test_model_then_new_reports_configured_ivan(monkeypatch):
    """/model persists to config.yaml and /new drops the override: /limits must follow config."""
    calls = []
    result = _handle_without_override(monkeypatch, {"model": {"provider": IVAN}}, calls)
    assert calls == ["ivan"]
    assert result.startswith("Claude Ivan") and "12%" in result


def test_configured_old_route_stays_blocked(monkeypatch):
    assert "default" not in limits.account_helper().USAGE_ENABLED_ACCOUNTS
    calls = []
    result = _handle_without_override(monkeypatch, {"model": {"provider": OLD}}, calls)
    assert calls == []
    assert "Отключён владельцем" in result


def test_configured_unknown_route_fail_closed(monkeypatch):
    calls = []
    assert _handle_without_override(monkeypatch, {"model": {"provider": "anthropic"}}, calls) == limits.UNAVAILABLE
    assert calls == []


def test_runtime_bound_session_and_private_target(monkeypatch):
    import sys
    import hermes_constants
    import hermes_cli.config
    from gateway import session_context as sc
    calls = []
    async def send(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(success=True)
    runner = SimpleNamespace(_session_model_override=lambda key: {"provider": IVAN},
        _authorization_adapter=lambda platform, profile: SimpleNamespace(send=send))
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=lambda: runner))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: limits.DEFAULT_HOME)
    monkeypatch.setattr(hermes_cli.config, "load_config", lambda: {"limits": {"owner_ids": {"telegram": "123"}}})
    monkeypatch.setattr(limits, "dispatch", limits.dispatch)  # real boundary, fake only fetch I/O below
    monkeypatch.setattr(limits, "safe_usage", lambda a, f: limits.sanitize_summary(a, ROW))
    tokens = sc.set_session_vars(platform="telegram", user_id="123", chat_id="-100999", chat_type="group",
                                 thread_id="77", profile="default", session_key="this-session")
    try:
        result = asyncio.run(limits.handle("all"))
        assert "отправлен" in result
        assert calls[0]["chat_id"] == "123"
        assert calls[0]["reply_to"] is None and calls[0]["metadata"] == {}
        assert "12%" not in result
    finally:
        sc.clear_session_vars(tokens)
    monkeypatch.setenv("HERMES_SESSION_KEY", "foreign")
    sc.reset_session_vars()
    assert asyncio.run(limits.handle("all")) == limits.UNAVAILABLE


def test_other_profile_denied(monkeypatch):
    import sys
    import hermes_constants
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_gateway_runner_ref=lambda: None))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: Path("/home/hun/.hermes/profiles/other"))
    assert asyncio.run(limits.handle("")) == limits.UNAVAILABLE
