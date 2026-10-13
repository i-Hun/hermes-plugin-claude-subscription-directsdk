"""Claude Subscription DirectSDK (Experimental) — standalone Hermes model-provider registration."""
import logging
from dataclasses import fields
import os

from providers import register_provider
from providers.base import ProviderProfile

# Dual import: the Hermes loader imports this directory as a package; the flat test path does not.
try:
    from .model_catalog import ALIASES, MODEL_METADATA, native_model
    from .directsdk_setup import INSTALL_HINT, _resolve
    from .claude_accounts import AccountError
except ImportError:
    from model_catalog import ALIASES, MODEL_METADATA, native_model
    from directsdk_setup import INSTALL_HINT, _resolve
    from claude_accounts import AccountError

logger = logging.getLogger(__name__)


class ClaudeOAuthDirectSDKProfile(ProviderProfile):
    model_metadata = MODEL_METADATA

    def get_model_context_length(self, model):
        route = native_model(model)
        pinned = self.model_metadata.get(route, {}).get('context_window')
        # Unpinned: behind the relay native Claude Code runs a plain id within its 200K default, and
        # Hermes' own family guess (claude-opus-5-5 -> 1M before it was pinned) would outgrow that.
        # A [1m] id stays unreported: no Hermes guess exceeds the 1M native budget, nor is it promised.
        return pinned or (None if route.endswith('[1m]') else 200_000)

    def get_usage_cost(self, model, usage):
        from decimal import Decimal, InvalidOperation
        from agent.usage_pricing import CostResult, format_cost_label

        native = (usage.raw_usage or {}).get('native_cost') or {}
        unknown = CostResult(amount_usd=None, status='unknown', source='none', label='n/a',
                             notes=('native final list-price accounting unavailable; subscription invoice unknown',))
        amount = native.get('total_cost_usd')
        models = native.get('modelUsage') or {}
        if isinstance(amount, bool) or not models or any(row.get('costBasis') != 'list' for row in models.values()):
            return unknown
        try:
            amount = Decimal(str(amount))
        except InvalidOperation:
            return unknown
        if not amount.is_finite() or amount < 0:
            return unknown
        return CostResult(amount_usd=amount, status='estimated', source='provider_cost_api',
                          label=format_cost_label(amount), notes=('native API list-price equivalent; not subscription invoice; extra usage unknown',))

    def create_client(self, **client_kwargs):
        try:
            from .directsdk import Client
        except ImportError:
            from directsdk import Client
        return Client(**client_kwargs)

    def fetch_models(self, **kwargs):
        # No HTTP /models endpoint: the account's own picker (CLI `initialize` handshake) is the
        # live list for /model, the Desktop picker and `hermes model`; None degrades to the catalog.
        rows = self.discover_models(**kwargs)
        return [row["id"] for row in rows] if rows else None

    def setup_status(self, **kwargs):
        try:
            from .directsdk_setup import setup_status
        except ImportError:
            from directsdk_setup import setup_status
        return setup_status(**kwargs)

    def discover_models(self, **kwargs):
        try:
            from .directsdk_setup import discover_models
        except ImportError:
            from directsdk_setup import discover_models
        return discover_models(**kwargs)

    def build_api_kwargs_extras(self, *, reasoning_config=None, **_):
        return ({'reasoning': dict(reasoning_config)} if reasoning_config else {}), {}


profile = ClaudeOAuthDirectSDKProfile(
    name='claude-subscription-directsdk-experimental',
    display_name='Claude Subscription DirectSDK (Experimental)',
    description='Claude Subscription DirectSDK (Experimental) (Claude Pro/Max subscription via your Claude Code login; Hermes owns tools)',
    api_mode='chat_completions',
    auth_type='external_process',
    supports_health_check=False,
    native_reasoning_details_type='claude-subscription-directsdk-experimental.native_assistant',
    env_vars=(),
    base_url='process://claude-subscription-directsdk-experimental',
    process_command='claude',
    process_args=(),
    process_command_env_vars=('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND',),
    default_aux_model='claude-sonnet-5[1m]',
    fallback_models=tuple(MODEL_METADATA),
    model_aliases={alias: native_model(alias) for alias in ALIASES},
)
register_provider(profile)


def _classify_isolated_error(*args, **kwargs):
    # AccountError must not be ValueError/TypeError: core's local validation
    # recovery overrides unknown verdicts for those exception types.
    return {'reason': 'unknown', 'retryable': False, 'should_fallback': False,
            'should_rotate_credential': False, 'should_compress': False}


class ClaudeIvanProfile(ClaudeOAuthDirectSDKProfile):
    """Explicit account route; never consult the old account or mutate process env."""

    def _account_env(self, env=None, require_login=True):
        try:
            from .claude_accounts import account_env
        except ImportError:
            from claude_accounts import account_env
        return account_env('ivan', env, require_login=require_login)

    def create_client(self, **client_kwargs):
        client_kwargs['account'] = 'ivan'
        return super().create_client(**client_kwargs)

    def setup_status(self, **kwargs):
        from pathlib import Path
        import sys
        login = [sys.executable, str(Path(__file__).with_name('claude_ivan_login.py'))]
        try:
            kwargs['env'] = self._account_env(kwargs.get('env'))
        except AccountError:
            return {'available': True, 'logged_in': False, 'plan': '',
                    'detail': 'Claude Ivan: independent local login required.', 'login_command': login}
        result = super().setup_status(**kwargs)
        result['login_command'] = login
        return result

    def discover_models(self, **kwargs):
        try:
            kwargs['env'] = self._account_env(kwargs.get('env'))
        except AccountError:
            return None
        return super().discover_models(**kwargs)


# Registered in every Hermes profile so /model is identical fleet-wide (owner decision
# 13.10.2026). The account is host-wide (one native config dir, see claude_accounts.
# ACCOUNT_DIRS), not per profile; nothing here reads the profile's HERMES_HOME. The
# original route and its ambient compatibility stay intact. /limits stays default-only
# (limits_command.py guards its own home).
ivan_profile = ClaudeIvanProfile(**{f.name: getattr(profile, f.name) for f in fields(profile)})
ivan_profile.name = 'claude-subscription-directsdk-ivan'
ivan_profile.display_name = 'Claude Subscription — Ivan (isolated)'
ivan_profile.description = 'Independent Claude Code login; no account rotation or credential fallback'
ivan_profile.base_url = 'process://claude-subscription-directsdk-ivan'
ivan_profile.process_command_env_vars = ()
ivan_profile.classify_api_error = _classify_isolated_error
register_provider(ivan_profile)

# The provider stays registered when Claude Code is missing so `hermes model` can show the
# install hint; the request path (`directsdk.Client`) refuses with the same message.
if _resolve(None, os.environ) is None:
    logger.warning("%s: %s", profile.display_name, INSTALL_HINT)
