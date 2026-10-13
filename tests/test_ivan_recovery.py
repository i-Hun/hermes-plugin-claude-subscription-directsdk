"""Regression for core overriding no-fallback on unclassified local ValueErrors."""
import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_directsdk import FAKE
from test_ivan_provider import isolated_provider  # noqa: F401


@pytest.mark.parametrize('failure', ['auth', 'quota', 'timeout', 'removed_credentials'])
@pytest.mark.parametrize('retry_count', [1, 3])
def test_real_core_recovery_never_falls_back(isolated_provider, tmp_path, monkeypatch,
                                            failure, retry_count):
    from providers import register_provider
    from agent.error_classifier import classify_api_error
    import agent.turn_api_error as recovery

    ivan, accounts, dirs = isolated_provider
    ivan.classify_api_error = sys.modules[type(ivan).__module__]._classify_isolated_error
    register_provider(ivan)
    native = tmp_path / 'fake-recovery.py'
    native.write_text(FAKE)
    env = {'PATH': os.defpath}
    if failure == 'timeout':
        env['HANG'] = '1'
    elif failure != 'removed_credentials':
        code = 'authentication_failed' if failure == 'auth' else 'rate_limit'
        env['NATIVE_ERROR'] = code + ':fake-secret private@example.test'
    client = ivan.create_client(command=[sys.executable, str(native)], env=env, timeout=1)
    if failure == 'removed_credentials':
        (dirs['ivan'] / '.credentials.json').unlink()
    try:
        with pytest.raises(accounts.AccountError) as caught:
            client.create(model='haiku', messages=[{'role': 'user', 'content': 'fixture'}])
    finally:
        client.close()
    error = caught.value
    assert 'fake-secret' not in str(error) and 'private@' not in str(error)
    classified = classify_api_error(error, provider=ivan.name)
    agent = SimpleNamespace(
        provider=ivan.name, model='haiku', base_url=ivan.base_url,
        fallback_model={'provider': 'claude-subscription-directsdk-experimental', 'model': 'haiku'},
        _has_pending_fallback=lambda: True,
        _buffer_diagnostic_status=Mock(),
        _try_activate_fallback=Mock(side_effect=AssertionError('cross-account fallback')),
    )
    # Only terminal presentation/persistence is replaced. Classification and every
    # fallback/retry decision above it are the actual installed core functions.
    monkeypatch.setattr(recovery, 'settle_delivered_partial', lambda *a: '')
    terminal = Mock(return_value={'error': str(error)})
    monkeypatch.setattr(recovery, 'nonretryable_client_error_result', terminal)
    kwargs: dict = {key: None for key in inspect.signature(recovery.settle_unrecovered_error).parameters
              if key != 'agent'}
    kwargs.update(api_error=error, classified=classified, _retry=SimpleNamespace(),
                  is_context_length_error=False, is_rate_limited=False,
                  _is_zai_coding_overload=False, _provider=ivan.name, _base=ivan.base_url,
                  _model='haiku', messages=[], api_messages=[], api_kwargs={},
                  conversation_history=[], approx_tokens=1, retry_count=retry_count,
                  max_retries=3, compression_attempts=0, api_call_count=1)
    verdict = recovery.settle_unrecovered_error(agent, **kwargs)
    assert verdict.action == 'return'
    terminal.assert_called_once()
    agent._try_activate_fallback.assert_not_called()
