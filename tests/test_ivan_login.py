import importlib.util
from pathlib import Path
from unittest.mock import Mock
import pytest

spec = importlib.util.spec_from_file_location('ivan_login_test', Path(__file__).resolve().parents[1] / 'claude_ivan_login.py')
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


def test_scope_launch_no_secret_argv(monkeypatch):
    monkeypatch.setattr(login.sys, 'argv', ['claude_ivan_login.py', 'login'])
    call = Mock(return_value=0)
    monkeypatch.setattr(login.subprocess, 'call', call)
    monkeypatch.setattr(login.os, 'umask', lambda mask: 0o077)
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'fake-secret')
    assert login.main() == 0
    args = call.call_args.args[0]
    assert args[:5] == ['systemd-run','--user','--scope','--slice=agents.slice','--quiet']
    assert args[-2:] == ['--inside','login']
    assert 'fake-secret' not in str(args)


def test_inside_refuses_wrong_cgroup(monkeypatch):
    monkeypatch.setattr(login.sys, 'argv', ['claude_ivan_login.py', '--inside', 'login'])
    monkeypatch.setattr(login.os, 'umask', lambda mask: 0o077)
    monkeypatch.setattr(Path, 'read_text', lambda path: '0::/app.slice/wrong.scope')
    monkeypatch.setattr(login, 'prepare_directory', lambda: pytest.fail('must refuse before auth setup'))
    assert login.main() == 1


def test_private_directory_and_alias_rejection(tmp_path, monkeypatch):
    paths = {'ivan': tmp_path / 'ivan', 'default': tmp_path / 'default'}
    paths['default'].mkdir()
    monkeypatch.setattr(login, 'ACCOUNT_DIRS', paths)
    login.prepare_directory()
    assert paths['ivan'].stat().st_mode & 0o777 == 0o700
    assert list(paths['ivan'].iterdir()) == []
    paths['ivan'].rmdir()
    paths['ivan'].symlink_to(paths['default'], target_is_directory=True)
    with pytest.raises(login.AccountError):
        login.prepare_directory()
