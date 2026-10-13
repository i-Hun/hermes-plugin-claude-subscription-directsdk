# Local Claude accounts (all Hermes profiles)

Local-only deployment, not an upstream/catalog publication. Plugin version 0.3.2.

## Routes

- `claude-subscription-directsdk-experimental`: original route retained but disabled by the owner's shutdown policy in all Hermes profiles. Old credentials are privately backed up, not deleted.
- `claude-subscription-directsdk-ivan`: independent native config at `/home/hun/.claude-ivan`.
  Registered in every Hermes profile (identical /model fleet-wide, 13.10.2026); the account
  is host-wide, not per profile. `/limits` remains default-only.

Ivan strips ambient Anthropic/Claude/backend authentication variables from a private child env.
There is no process-global env edit, copied credential, helper refresh, fallback token or rotation.
Native Claude owns refresh, locks and caches under its selected config directory. The directory
must be owned by this UID with mode 700 and credentials must be a regular, singly linked file
with mode 600. Login is checked both at client construction and before each request. Missing
login and request errors fail closed. The provider explicitly disables Hermes retry/fallback
and credential rotation for this route: an outage requires a manual retry or explicit /model
choice, never automatic use of the old account.

The original provider keeps its native carrier format for history compatibility; route identity
is separate (`process://claude-subscription-directsdk-ivan`). Both windows and discovery inherit
the original pinned metadata; no picker rows are filtered to the pinned catalog.

## Human login (on this host in a real terminal)

    python3 /home/hun/.hermes/plugins/claude-subscription-directsdk-experimental/claude_ivan_login.py

The wrapper launches a scope in `agents.slice`, checks its actual cgroup, sets umask 077,
then execs official `claude auth login --claudeai`. Use the second account in the browser.
Any password/OTP/callback code stays in the official browser/local terminal, NOT in Hermes chat.
Do not use `/claude-login code` for this account. No old credentials are copied.

After login:

    python3 /home/hun/.hermes/plugins/claude-subscription-directsdk-experimental/claude_ivan_login.py status
    python3 /home/hun/.hermes/plugins/claude-subscription-directsdk-experimental/claude_ivan_login.py probe

`probe` makes one small real Haiku request and prints only success, never response contents.
Status/probe do not print native errors or identity information. Permissions failures are
fail-closed; inspect and repair permissions locally rather than copying old credentials.

## Limits

Default config already aliases `/limits` to the default-only `safe-account-limits` plugin command
`/account-limits`. Handler implementation: `limits_command.py`.

- `/limits`: only the explicitly selected Claude route in this session (memory, then persisted
  session override). Unknown/non-Claude/unbound context returns unavailable, never guesses from
  process env or another session. Selecting the disabled old route reports that it is disabled,
  without reading credentials or making a usage request.
- `/limits all`: owner ID pinned at `limits.owner_ids.telegram` in config (integer or numeric
  string); runtime authorization does NOT use username, display name, admins or allowlists.
  Only Ivan is eligible for an on-demand usage read; the old account is labelled disabled.
  `claude_accounts.USAGE_ENABLED_ACCOUNTS` enforces this before credentials/HTTP, independently
  of provider configuration and even if old credentials reappear. The summary goes only to that Telegram user's DM,
  without a group thread or reply anchor. Failure to DM returns only a fixed refusal.
- Read-only usage does not refresh credentials or poll in the background. Expired credentials
  report unavailable; the isolated native login/request owns refresh.
- Output allowlist: account label, numeric percentage, validated timezone-aware reset timestamp.
  No raw API body, email, plan identity, token, or upstream exception.

Accounts share host/VPN egress; this does not hide a shared IP from Anthropic.

## Verification and deployment status

See `verification/result.md`, `installed-probe.log`, `mutations.log` and exact source manifest.
Installed loader was exercised in a fresh process with the same `hermes_bootstrap` dependency
activation as the real launcher. Existing processes were not force-restarted; models of live
sessions were not changed. Review must precede any requested safe gateway restart. After approval,
use the existing idle/draining restart path, then verify the live service, rather than killing
active turns. The second account is not connected until the human login and live probe succeed.
