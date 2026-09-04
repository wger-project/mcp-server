"""Auth for incoming MCP requests and the outbound wger credential.

Inbound (``MCP_AUTH``):

- ``oidc`` — validate an SSO (OIDC) token; carry it for token-exchange.
- ``static_token`` — single-user; validate a shared secret, static dev token
  outbound. Authenticated, so safe to expose over TLS.
- ``none`` — local-dev only; no inbound auth, static dev token outbound.

Outbound is always a per-request wger credential supplied by a
:class:`WgerTokenProvider` (see ``exchange.py`` and ``docs/adr/0001``).
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from ..config import AuthStrategy, Settings
from .asfacade import (
    AS_METADATA_PATH,
    AUTHORIZE_PATH,
    TOKEN_PATH,
    AuthorizationServerFacade,
)
from .base import NoAuthMiddleware, StaticTokenMiddleware
from .exchange import TokenExchanger, WgerTokenProvider
from .oauth import (
    WELL_KNOWN_PATH,
    forwarded_origin,
    protected_resource_metadata,
    resource_identifier,
    resource_metadata_url,
)
from .oidc import OidcAuthMiddleware
from .oidc_discovery import OidcEndpoints, discover_endpoints

__all__ = [
    "AS_METADATA_PATH",
    "AUTHORIZE_PATH",
    "TOKEN_PATH",
    "WELL_KNOWN_PATH",
    "AuthorizationServerFacade",
    "NoAuthMiddleware",
    "OidcAuthMiddleware",
    "StaticTokenMiddleware",
    "TokenExchanger",
    "WgerTokenProvider",
    "build_auth_middleware",
    "build_authorization_server_facade",
    "build_token_provider",
    "forwarded_origin",
    "protected_resource_metadata",
    "resource_identifier",
    "resource_metadata_url",
]


def _resolve_endpoints(s: Settings) -> OidcEndpoints:
    return discover_endpoints(
        str(s.oidc_issuer),
        jwks_uri=str(s.oidc_jwks_uri) if s.oidc_jwks_uri else None,
        token_endpoint=str(s.oidc_token_endpoint) if s.oidc_token_endpoint else None,
        authorization_endpoint=(
            str(s.oidc_authorization_endpoint) if s.oidc_authorization_endpoint else None
        ),
    )


def _secret(value: SecretStr | None) -> str:
    """Unwrap a secret setting. str() on a SecretStr yields the mask, not the value."""
    return value.get_secret_value() if value is not None else ""


def build_auth_middleware(settings: Settings) -> tuple[type, dict[str, Any]]:
    """Pick an inbound auth middleware class + kwargs based on settings."""
    s = settings
    match s.mcp_auth:
        case AuthStrategy.none:
            return NoAuthMiddleware, {}
        case AuthStrategy.static_token:
            return StaticTokenMiddleware, {"token": _secret(s.mcp_static_token)}
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
                # Only pin the metadata URL when MCP_PUBLIC_URL is explicit;
                # otherwise the middleware derives it per-request from the
                # reverse-proxy forwarded headers (avoids a baked-in 0.0.0.0).
                "resource_metadata_url": (
                    resource_metadata_url(s) if s.mcp_public_url else None
                ),
                # Facade endpoints are public (they carry their own OAuth client
                # auth); bypass inbound-token validation for the configured paths.
                "public_paths": {s.oauth_authorize_path, s.oauth_token_path},
            }
    raise RuntimeError(f"unsupported MCP_AUTH: {s.mcp_auth}")  # pragma: no cover


def build_authorization_server_facade(
    settings: Settings,
) -> AuthorizationServerFacade | None:
    """Build the AS facade for OIDC mode (None otherwise).

    Lets claude.ai-style clients, which treat this origin as the authorization
    server, reach a private IdP: ``/authorize`` 302s to the IdP and
    ``/token`` reverse-proxies to it (paths configurable, see ``config``).
    """
    if settings.mcp_auth is not AuthStrategy.oidc:
        return None
    eps = _resolve_endpoints(settings)
    return AuthorizationServerFacade(
        idp_authorization_endpoint=eps.authorization_endpoint,
        idp_token_endpoint=eps.token_endpoint,
        authorize_path=settings.oauth_authorize_path,
        token_path=settings.oauth_token_path,
    )


def build_token_provider(settings: Settings) -> WgerTokenProvider:
    """Build the outbound wger credential provider for the chosen strategy."""
    s = settings
    if s.mcp_auth in (AuthStrategy.none, AuthStrategy.static_token):
        # Both single-user strategies call wger with the same personal API key;
        # they differ only in whether inbound requests are authenticated.
        return WgerTokenProvider(dev_token=_secret(s.wger_dev_token))
    token_endpoint = _resolve_endpoints(s).token_endpoint
    exchanger = TokenExchanger(
        token_endpoint=token_endpoint,
        client_id=str(s.oidc_client_id),
        client_secret=_secret(s.oidc_client_secret),
        wger_audience=str(s.wger_oidc_audience),
        provider_token_url=s.provider_token_url,
        provider=s.wger_allauth_provider,
    )
    return WgerTokenProvider(exchanger=exchanger)
