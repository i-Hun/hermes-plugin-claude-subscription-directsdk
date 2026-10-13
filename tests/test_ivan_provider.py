"""Offline integration: real child processes, isolated fake credentials, no live auth."""
import importlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path

import pytest
from test_directsdk import FAKE


@pytest.fixture
def isolated_provider(profile, tmp_path, monkeypatch):
    module = sys.modules[type(profile).__module__]
    ivan = module.ClaudeIvanProfile(**{f.name: getattr(profile, f.name) for f in fields(profile)})
    ivan.name = 'claude-subscription-directsdk-ivan'
    ivan.base_url = 'process://' + ivan.name
    accounts = importlib.import_module(module.__name__ + '.claude_accounts')
    dirs = {name: tmp_path / name for name in ('default', 'ivan')}
    for name, directory in dirs.items():
        directory.mkdir(mode=0o700)
        path = directory / '.credentials.json'
        path.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fake-' + name, 'expiresAt': 9999999999999}}))
        path.chmod(0o600)
    monkeypatch.setattr(accounts, 'ACCOUNT_DIRS', dirs)
    return ivan, accounts, dirs


def test_both_provider_identity_and_windows(isolated_provider, profile):
    ivan, _, _ = isolated_provider
    assert ivan.name != profile.name and ivan.base_url != profile.base_url
    for model in profile.fallback_models:
        assert ivan.get_model_context_length(model) == profile.get_model_context_length(model)
    assert ivan.get_model_context_length('unknown-future-id') == 200000


def test_missing_login_client_and_discovery_fail_closed(isolated_provider, monkeypatch):
    ivan, accounts, dirs = isolated_provider
    (dirs['ivan'] / '.credentials.json').unlink()
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'fake-old')
    with pytest.raises(accounts.AccountError):
        ivan.create_client()
    assert ivan.discover_models() is None
    status = ivan.setup_status()
    assert status['logged_in'] is False
    assert status['login_command'][-1].endswith('claude_ivan_login.py')


def test_two_concurrent_children_refresh_only_their_config(isolated_provider, profile, tmp_path):
    ivan, _, dirs = isolated_provider
    native = tmp_path / 'fake.py'
    injection = """d=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
p=d/'.credentials.json'
x=json.loads(p.read_text()); x['refreshed_by_fake_child']=True; p.write_text(json.dumps(x))
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({k:os.environ.get(k) for k in ('CLAUDE_CONFIG_DIR','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_API_KEY')}))
rows=[]"""
    native.write_text(FAKE.replace('rows=[]', injection))
    before = dict(os.environ)
    old_bytes = (dirs['default'] / '.credentials.json').read_bytes()
    captures = [tmp_path / 'old.json', tmp_path / 'ivan.json']
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'CLAUDE_CONFIG_DIR': str(dirs['default'])}
    old = profile.create_client(command=[sys.executable, str(native)], env={**env, 'CAPTURE': str(captures[0])})
    new = ivan.create_client(command=[sys.executable, str(native)], env={**env, 'CAPTURE': str(captures[1]),
        'CLAUDE_CODE_OAUTH_TOKEN': 'fake-ambient', 'ANTHROPIC_API_KEY': 'fake-key'})
    def invoke(client):
        return client.create(model='haiku', messages=[{'role':'user', 'content':'fixture'}],
            tools=[{'type':'function','function':{'name':'probe','description':'TAIL',
                    'parameters':{'type':'object','properties':{'value':{'type':'string'}}}}}])
    try:
        invoke(new)
        assert (dirs['default'] / '.credentials.json').read_bytes() == old_bytes
        assert json.loads((dirs['ivan'] / '.credentials.json').read_text())['refreshed_by_fake_child'] is True
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(invoke, [old, new]))
        for name, path in zip(('default', 'ivan'), captures):
            observed = json.loads(path.read_text())
            assert observed['CLAUDE_CONFIG_DIR'] == str(dirs[name])
            assert observed['CLAUDE_CODE_OAUTH_TOKEN'] is None
            assert observed['ANTHROPIC_API_KEY'] is None
        assert new.base_url == ivan.base_url and old.base_url == profile.base_url
        assert dict(os.environ) == before
    finally:
        old.close()
        new.close()


def test_login_is_rechecked_before_request(isolated_provider):
    ivan, accounts, dirs = isolated_provider
    client = ivan.create_client(command='never-spawn')
    (dirs['ivan'] / '.credentials.json').unlink()
    try:
        with pytest.raises(accounts.AccountError):
            client.create(model='haiku', messages=[{'role':'user','content':'fixture'}])
    finally:
        client.close()


def test_picker_uses_isolated_environment(isolated_provider, monkeypatch):
    ivan, _, dirs = isolated_provider
    module = sys.modules[type(ivan).__module__]
    setup = importlib.import_module(module.__name__ + '.directsdk_setup')
    seen = []
    def discover(**kwargs):
        seen.append(kwargs['env'])
        return [{'id': 'unpinned-new-model'}]
    monkeypatch.setattr(setup, 'discover_models', discover)
    assert ivan.fetch_models(env={'CLAUDE_CODE_OAUTH_TOKEN':'fake-old'}) == ['unpinned-new-model']
    assert seen[0]['CLAUDE_CONFIG_DIR'] == str(dirs['ivan'])
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in seen[0]


def test_native_auth_error_does_not_leak_or_offer_old_login(isolated_provider, tmp_path):
    ivan, accounts, _ = isolated_provider
    native = tmp_path / 'fake-error.py'
    native.write_text(FAKE)
    client = ivan.create_client(command=[sys.executable, str(native)],
        env={'PATH': os.defpath, 'NATIVE_ERROR': 'authentication_failed:fake-secret private@example.test'})
    try:
        with pytest.raises(accounts.AccountError) as error:
            client.create(model='haiku', messages=[{'role':'user','content':'fixture'}])
        assert 'fake-secret' not in str(error.value) and 'private@' not in str(error.value)
        assert 'CLAUDE_CODE_OAUTH_TOKEN' not in str(error.value)
    finally:
        client.close()


def test_ivan_registers_in_any_hermes_profile(profile, tmp_path, monkeypatch):
    """/model must be identical fleet-wide: discovery under a non-default HERMES_HOME
    (the fixture's tmp home) registers the Ivan route, and its runtime env is bound to
    the host-wide account dir, never to that profile's home."""
    import providers

    home = Path(os.environ['HERMES_HOME']).resolve()
    assert home != Path('/home/hun/.hermes')
    ivan = providers.get_provider_profile('claude-subscription-directsdk-ivan')
    assert ivan is not None, 'Ivan route missing outside the default profile'
    assert ivan.base_url == 'process://claude-subscription-directsdk-ivan'
    assert ivan.process_command_env_vars == ()
    module = sys.modules[type(ivan).__module__]
    accounts = importlib.import_module(module.__name__ + '.claude_accounts')
    assert accounts.ACCOUNT_DIRS['ivan'] == Path('/home/hun/.claude-ivan')
    fake = tmp_path / 'host-ivan'
    fake.mkdir(mode=0o700)
    cred = fake / '.credentials.json'
    cred.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fake', 'expiresAt': 9999999999999}}))
    cred.chmod(0o600)
    monkeypatch.setitem(accounts.ACCOUNT_DIRS, 'ivan', fake)
    client = ivan.create_client(command='never-spawn',
                                env={'PATH': os.defpath, 'CLAUDE_CODE_OAUTH_TOKEN': 'old'})
    try:
        assert client.env['CLAUDE_CONFIG_DIR'] == str(fake)
        assert 'CLAUDE_CODE_OAUTH_TOKEN' not in client.env
        assert str(home) not in client.env['CLAUDE_CONFIG_DIR']
    finally:
        client.close()
