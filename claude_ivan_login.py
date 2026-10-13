#!/usr/bin/env python3
"""Human-operated local auth; never send auth codes to Hermes/chat.

Run on this host in a real terminal. Native Claude opens the browser and owns
its callback/terminal input. This wrapper never captures login output or stores
codes, refresh tokens, fallback tokens or shared login state.
"""
import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.request

from claude_accounts import ACCOUNT_DIRS, AccountError, account_env
from directsdk_setup import _resolve


def prepare_directory():
    path = ACCOUNT_DIRS['ivan']
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
            or path.resolve() == ACCOUNT_DIRS['default'].resolve()):
        raise AccountError('Independent login directory must be owned by this user with mode 700.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['login', 'status', 'probe', 'network-check'], default='login')
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if not args.inside:
        command = ['systemd-run', '--user', '--scope', '--slice=agents.slice', '--quiet',
                   sys.executable, str(Path(__file__).resolve()), '--inside', args.action]
        return subprocess.call(command)
    cgroup = Path('/proc/self/cgroup').read_text()
    if '/agents.slice/' not in cgroup:
        print('Refusing to run outside agents.slice.', file=sys.stderr)
        return 1
    if args.action == 'network-check':
        print('cgroup: ' + cgroup.strip())
        with urllib.request.urlopen('https://api.ipify.org', timeout=20) as response:
            import ipaddress
            print('egress: ' + str(ipaddress.ip_address(response.read(128).decode().strip())))
        return 0
    prepare_directory()
    env = account_env('ivan', require_login=args.action != 'login')
    command = _resolve(None, env)
    if command is None:
        raise AccountError('Claude Code executable unavailable.')
    if args.action == 'login':
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise AccountError('Run this login yourself in a local terminal; never paste codes into chat.')
        os.chdir(ACCOUNT_DIRS['ivan'])
        os.execvpe(command[0], command + ['auth', 'login', '--claudeai'], env)
    if args.action == 'status':
        from directsdk_setup import setup_status
        status = setup_status(command=command, env=env)
        print('Claude Ivan: ' + ('logged in' if status['logged_in'] else 'login required'))
        return 0 if status['logged_in'] else 1
    from directsdk import Client
    client = Client(command=command, env=env, account='ivan', timeout=90)
    try:
        response = client.create(model='claude-haiku-4-5-20251001',
                                 messages=[{'role': 'user', 'content': 'Reply with OK.'}], max_tokens=32)
        if not response.choices or not response.choices[0].message.content:
            raise AccountError('No live response.')
        print('Claude Ivan: live response received (content not logged).')
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Claude Ivan operation failed; check independent login and permissions locally.', file=sys.stderr)
        raise SystemExit(1)
