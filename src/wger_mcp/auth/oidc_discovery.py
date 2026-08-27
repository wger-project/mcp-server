"""OIDC discovery: resolve the endpoints of whichever provider issues the tokens.

Reads ``{issuer}/.well-known/openid-configuration`` so the server is not tied
to a specific provider's URL layout (wger itself, Keycloak, Authentik, Auth0,
Okta, …). Explicit overrides win and skip the network call. Resolution is a
one-off, synchronous call done at startup.
"""

from __future__ import annotations

from typing import NamedTuple

import httpx


class OidcDiscoveryError(RuntimeError):
    pass


class OidcEndpoints(NamedTuple):
    """The endpoints this server may need from a provider.

    The first three are required — resolution fails without them. The last two
    are informational: ``userinfo_endpoint`` is only used to name the caller,
    and ``registration_endpoint`` is present exactly when the provider offers
    dynamic client registration (allauth omits it while DCR is off), which is
    how the AS facade decides whether to advertise and proxy ``/register``.
    Both are ``None`` when every required endpoint was given explicitly, since
    the document is then never fetched.
    """

    jwks_uri: str
    token_endpoint: str
    authorization_endpoint: str
    userinfo_endpoint: str | None = None
    registration_endpoint: str | None = None


def discover_endpoints(
    issuer: str,
    *,
    jwks_uri: str | None = None,
    token_endpoint: str | None = None,
    authorization_endpoint: str | None = None,
    timeout: float = 10.0,
) -> OidcEndpoints:
    """Return the endpoints for ``issuer``.

    Uses explicit overrides where given; otherwise fetches the provider's
    discovery document once. Raises :class:`OidcDiscoveryError` if a needed
    value can't be resolved.
    """
    if jwks_uri and token_endpoint and authorization_endpoint:
        return OidcEndpoints(jwks_uri, token_endpoint, authorization_endpoint)

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        doc = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcDiscoveryError(f"OIDC discovery failed for {url}: {exc}") from exc

    resolved_jwks = jwks_uri or doc.get("jwks_uri")
    resolved_token = token_endpoint or doc.get("token_endpoint")
    resolved_authz = authorization_endpoint or doc.get("authorization_endpoint")
    if not resolved_jwks or not resolved_token or not resolved_authz:
        raise OidcDiscoveryError(
            f"discovery document at {url} is missing "
            "jwks_uri/token_endpoint/authorization_endpoint"
        )
    return OidcEndpoints(
        resolved_jwks,
        resolved_token,
        resolved_authz,
        doc.get("userinfo_endpoint"),
        doc.get("registration_endpoint"),
    )
