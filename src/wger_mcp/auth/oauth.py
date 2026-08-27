"""OAuth 2.0 Protected Resource Metadata (RFC 9728) for MCP-native auth.

Interactive MCP clients (e.g. Claude) discover where to authenticate by
fetching ``/.well-known/oauth-protected-resource``. It names the authorization
server to run the OAuth flow against, and the scopes this resource expects the
resulting token to carry.
"""

from __future__ import annotations

from starlette.requests import Request

from ..config import AuthStrategy, Settings

WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"


def forwarded_origin(request: Request) -> str | None:
    """Public ``scheme://host`` origin inferred from reverse-proxy headers.

    Honours ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` (first value of each,
    as a proxy chain may append several), falling back to the request's own
    scheme and ``Host`` header. Returns ``None`` when no host can be determined.
    """

    def _first(value: str | None) -> str | None:
        return value.split(",")[0].strip() if value else None

    proto = _first(request.headers.get("x-forwarded-proto")) or request.url.scheme
    host = _first(request.headers.get("x-forwarded-host")) or request.headers.get("host")
    if not host:
        return None
    return f"{proto}://{host}"


def resource_identifier(settings: Settings, *, origin: str | None = None) -> str:
    """The canonical public URL clients use to reach this MCP server.

    Resolution order:

    1. ``MCP_PUBLIC_URL`` — explicit config, always wins.
    2. ``origin`` — derived from the request's reverse-proxy forwarded headers,
       so a deploy behind nginx needs no per-host config.
    3. ``host:port`` — dev fallback; yields ``0.0.0.0`` when bound to all
       interfaces, which is only useful for local testing.
    """
    if settings.mcp_public_url:
        return str(settings.mcp_public_url).rstrip("/")
    if origin:
        return origin.rstrip("/")
    return f"http://{settings.host}:{settings.port}".rstrip("/")


def resource_metadata_url(settings: Settings, *, origin: str | None = None) -> str:
    return resource_identifier(settings, origin=origin) + WELL_KNOWN_PATH


def authorization_server(settings: Settings, *, origin: str | None = None) -> str:
    """Which origin clients should run the OAuth flow against.

    Normally this server itself: it fronts the provider as an AS facade (see
    ``auth/asfacade.py``) because claude.ai and others drive ``/authorize`` and
    ``/token`` against the MCP origin regardless of what is advertised here, and
    a facade is also what lets a private IdP stay private.

    ``MCP_AS_FACADE=false`` points at the real provider instead — honest, and
    one hop shorter, for a deployment whose clients all follow the pointer.
    """
    if settings.mcp_as_facade:
        return resource_identifier(settings, origin=origin)
    issuer = (
        settings.wger_base_url
        if settings.mcp_auth is AuthStrategy.wger_oidc
        else settings.oidc_issuer
    )
    return str(issuer).rstrip("/")


def protected_resource_metadata(settings: Settings, *, origin: str | None = None) -> dict:
    meta = {
        "resource": resource_identifier(settings, origin=origin),
        "authorization_servers": [authorization_server(settings, origin=origin)],
        "bearer_methods_supported": ["header"],
    }
    # Only under wger_oidc do we know what the token has to carry: there the
    # scopes are wger's own and this server is the one asking for them. With an
    # external IdP the mapping is the deployment's business, so saying nothing
    # beats guessing.
    if settings.mcp_auth is AuthStrategy.wger_oidc:
        meta["scopes_supported"] = list(settings.mcp_wger_scopes)
    return meta
