"""Auth for incoming MCP requests and the outbound wger credential.

Inbound (``MCP_AUTH``):

- ``wger_oidc`` — wger 2.7+ is itself the OIDC provider; its opaque access token
  is carried through to the API unchanged. The recommended multi-user mode.
- ``oidc`` — validate a token from an *external* SSO IdP; carry it for
  token-exchange. For setups already fronted by one, and for wger < 2.7.
- ``static_token`` — single-user; validate a shared secret, static dev token
  outbound. Authenticated, so safe to expose over TLS.
- ``none`` — local-dev only; no inbound auth, static dev token outbound.

Outbound is always a per-request wger credential supplied by a
:class:`WgerTokenProvider` (see ``exchange.py``, ``docs/adr/0001`` and
``docs/adr/0005``).
"""

from __future__ import annotations

from typing import Any

from ..config import AuthStrategy, Settings
from .asfacade import (
    AS_METADATA_PATH,
    AUTHORIZE_PATH,
    REGISTER_PATH,
    TOKEN_PATH,
    AuthorizationServerFacade,
)
from .base import NoAuthMiddleware, StaticTokenMiddleware
from .exchange import TokenExchanger, WgerTokenProvider
from .oauth import (
    WELL_KNOWN_PATH,
    authorization_server,
    forwarded_origin,
    protected_resource_metadata,
    resource_identifier,
    resource_metadata_url,
)
from .oidc import OidcAuthMiddleware
from .oidc_discovery import OidcEndpoints, discover_endpoints
from .wger_oidc import WgerBearerMiddleware

__all__ = [
    "AS_METADATA_PATH",
    "AUTHORIZE_PATH",
    "REGISTER_PATH",
    "TOKEN_PATH",
    "WELL_KNOWN_PATH",
    "AuthorizationServerFacade",
    "NoAuthMiddleware",
    "OidcAuthMiddleware",
    "StaticTokenMiddleware",
    "TokenExchanger",
    "WgerBearerMiddleware",
    "WgerTokenProvider",
    "authorization_server",
    "build_auth_middleware",
    "build_authorization_server_facade",
    "build_token_provider",
    "forwarded_origin",
    "protected_resource_metadata",
    "reset_endpoint_cache",
    "resource_identifier",
    "resource_metadata_url",
    "uses_oauth",
]

#: Strategies whose callers authenticate with an OAuth provider, and for which
#: the discovery documents and the AS facade are therefore meaningful. Under
#: ``static_token``/``none`` advertising them would send clients through a flow
#: whose result this server never accepts.
_OAUTH_STRATEGIES = frozenset({AuthStrategy.wger_oidc, AuthStrategy.oidc})

#: Endpoint resolution memoised for the process. Discovery is one synchronous
#: fetch at startup, but three call sites want the answer; without this the
#: server would ask the provider three times before it accepts a request.
_endpoint_cache: dict[tuple[str | None, ...], OidcEndpoints] = {}


def reset_endpoint_cache() -> None:
    """Forget every memoised discovery result. For tests building many apps."""
    _endpoint_cache.clear()


def uses_oauth(settings: Settings) -> bool:
    """Whether callers authenticate against an OAuth provider under this strategy."""
    return settings.mcp_auth in _OAUTH_STRATEGIES and issuer_url(settings) is not None


def issuer_url(settings: Settings) -> str | None:
    """The provider that issues the tokens this server accepts.

    Under ``wger_oidc`` that is wger itself, which is why the mode needs no
    configuration beyond ``WGER_BASE_URL``.
    """
    if settings.mcp_auth is AuthStrategy.wger_oidc:
        return str(settings.wger_base_url)
    return str(settings.oidc_issuer) if settings.oidc_issuer else None


def _resolve_endpoints(s: Settings) -> OidcEndpoints:
    issuer = issuer_url(s)
    if issuer is None:  # pragma: no cover - config validation rules this out
        raise RuntimeError(f"MCP_AUTH={s.mcp_auth} has no token issuer configured")
    # The OIDC_*_ENDPOINT overrides apply to wger_oidc too, and are not merely a
    # convenience there: a deployment that reaches wger over an internal URL gets
    # internal URLs back from discovery, while /authorize is followed by the
    # user's *browser* and has to name the public one.
    key = (
        issuer,
        str(s.oidc_jwks_uri) if s.oidc_jwks_uri else None,
        str(s.oidc_token_endpoint) if s.oidc_token_endpoint else None,
        str(s.oidc_authorization_endpoint) if s.oidc_authorization_endpoint else None,
    )
    cached = _endpoint_cache.get(key)
    if cached is None:
        cached = discover_endpoints(
            key[0],
            jwks_uri=key[1],
            token_endpoint=key[2],
            authorization_endpoint=key[3],
        )
        _endpoint_cache[key] = cached
    return cached


def _facade_paths(settings: Settings) -> set[str]:
    """Facade endpoints, which carry their own OAuth client auth and so are public."""
    if not settings.mcp_as_facade or not uses_oauth(settings):
        return set()
    return {
        settings.oauth_authorize_path,
        settings.oauth_token_path,
        settings.oauth_register_path,
    }


def build_auth_middleware(settings: Settings) -> tuple[type, dict[str, Any]]:
    """Pick an inbound auth middleware class + kwargs based on settings."""
    s = settings
    # Only pin the metadata URL when MCP_PUBLIC_URL is explicit; otherwise the
    # middleware derives it per-request from the reverse-proxy forwarded headers
    # (avoids a baked-in 0.0.0.0).
    metadata_url = resource_metadata_url(s) if s.mcp_public_url else None
    match s.mcp_auth:
        case AuthStrategy.none:
            return NoAuthMiddleware, {}
        case AuthStrategy.static_token:
            return StaticTokenMiddleware, {"token": str(s.mcp_static_token)}
        case AuthStrategy.wger_oidc:
            return WgerBearerMiddleware, {
                "wger_base_url": str(s.wger_base_url),
                "allowed_users": set(s.mcp_oidc_allowed_users),
                "resource_metadata_url": metadata_url,
                "public_paths": _facade_paths(s),
            }
        case AuthStrategy.oidc:
            jwks_uri = _resolve_endpoints(s).jwks_uri
            return OidcAuthMiddleware, {
                "jwks_uri": jwks_uri,
                "issuer": str(s.oidc_issuer),
                "audience": s.mcp_oidc_audience,
                "algorithms": s.mcp_oidc_algorithms,
                "username_claim": s.mcp_oidc_username_claim,
                "allowed_users": set(s.mcp_oidc_allowed_users),
                "jwks_ttl_seconds": s.mcp_jwks_ttl_seconds,
                "resource_metadata_url": metadata_url,
                "public_paths": _facade_paths(s),
            }
    raise RuntimeError(f"unsupported MCP_AUTH: {s.mcp_auth}")  # pragma: no cover


def build_authorization_server_facade(
    settings: Settings,
) -> AuthorizationServerFacade | None:
    """Build the AS facade for the OAuth strategies (None otherwise).

    Lets claude.ai-style clients, which treat this origin as the authorization
    server, reach the provider: ``/authorize`` 302s to it, ``/token`` and
    ``/register`` reverse-proxy (paths configurable, see ``config``).
    """
    if not settings.mcp_as_facade or not uses_oauth(settings):
        return None
    eps = _resolve_endpoints(settings)
    native = settings.mcp_auth is AuthStrategy.wger_oidc
    return AuthorizationServerFacade(
        idp_authorization_endpoint=eps.authorization_endpoint,
        idp_token_endpoint=eps.token_endpoint,
        idp_registration_endpoint=eps.registration_endpoint if native else None,
        authorize_path=settings.oauth_authorize_path,
        token_path=settings.oauth_token_path,
        register_path=settings.oauth_register_path,
        # Only wger_oidc knows what the token must carry; with an external IdP
        # the scope names are the deployment's, so nothing is added or claimed.
        required_scopes=settings.mcp_wger_scopes if native else None,
        advertised_scopes=settings.mcp_wger_scopes if native else None,
    )


def build_token_provider(settings: Settings) -> WgerTokenProvider:
    """Build the outbound wger credential provider for the chosen strategy."""
    s = settings
    if s.mcp_auth is AuthStrategy.wger_oidc:
        # wger issued the caller's token and accepts it on its own API: there is
        # no credential to obtain, only one to forward.
        return WgerTokenProvider(pass_through=True)
    if s.mcp_auth in (AuthStrategy.none, AuthStrategy.static_token):
        # Both single-user strategies call wger with the same personal API key;
        # they differ only in whether inbound requests are authenticated.
        return WgerTokenProvider(dev_token=s.wger_dev_token)
    token_endpoint = _resolve_endpoints(s).token_endpoint
    exchanger = TokenExchanger(
        token_endpoint=token_endpoint,
        client_id=str(s.oidc_client_id),
        client_secret=str(s.oidc_client_secret),
        wger_audience=str(s.wger_oidc_audience),
        provider_token_url=s.provider_token_url,
        provider=s.wger_allauth_provider,
    )
    return WgerTokenProvider(exchanger=exchanger)
