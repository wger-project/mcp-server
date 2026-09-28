"""OIDC discovery: resolve the endpoints of whichever provider issues the tokens.

Reads ``{issuer}/.well-known/openid-configuration`` so the server is not tied
to a specific provider's URL layout (wger itself, Keycloak, Authentik, Auth0,
Okta, …). Explicit overrides win and skip the network call. Resolution is a
one-off, synchronous call done at startup.
"""

from __future__ import annotations

import logging
import time
from typing import NamedTuple

import httpx

log = logging.getLogger(__name__)

#: Pauses between attempts while the provider is not up yet — e.g. wger booting
#: next to this server in one compose file. Roughly half a minute in total.
_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)


class OidcDiscoveryError(RuntimeError):
    pass


class OidcEndpoints(NamedTuple):
    """The endpoints this server may need from a provider.

    The first three are required — resolution fails without them. The rest are
    ``None`` when every required endpoint was given explicitly, since the
    document is then never fetched. ``registration_endpoint`` is present exactly
    when the provider offers dynamic client registration (allauth omits it while
    DCR is off).
    """

    jwks_uri: str
    token_endpoint: str
    authorization_endpoint: str
    registration_endpoint: str | None = None
    token_endpoint_auth_methods: list[str] | None = None


def discover_endpoints(
    issuer: str,
    *,
    jwks_uri: str | None = None,
    token_endpoint: str | None = None,
    authorization_endpoint: str | None = None,
    timeout: float = 10.0,
    retry_delays: tuple[float, ...] = _RETRY_DELAYS,
) -> OidcEndpoints:
    """Return the endpoints for ``issuer``.

    Uses explicit overrides where given; otherwise fetches the provider's
    discovery document, retrying while the provider is unreachable or answers
    5xx. Raises :class:`OidcDiscoveryError` if a needed value can't be resolved.
    """
    if jwks_uri and token_endpoint and authorization_endpoint:
        return OidcEndpoints(jwks_uri, token_endpoint, authorization_endpoint)

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = _fetch_document(url, timeout, retry_delays)

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
        doc.get("registration_endpoint"),
        doc.get("token_endpoint_auth_methods_supported"),
    )


def _fetch_document(url: str, timeout: float, retry_delays: tuple[float, ...]) -> dict:
    for delay in (*retry_delays, None):
        try:
            resp = httpx.get(url, timeout=timeout)
            resp.raise_for_status()
            doc = resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                exc.response.status_code >= 500
            )
            if not retryable or delay is None:
                reason = str(exc) or type(exc).__name__
                raise OidcDiscoveryError(f"OIDC discovery failed for {url}: {reason}") from exc
            log.warning("OIDC discovery at %s failed (%s); retrying in %gs", url, exc, delay)
            time.sleep(delay)
            continue
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcDiscoveryError(f"OIDC discovery failed for {url}: {exc}") from exc
        if not isinstance(doc, dict):
            raise OidcDiscoveryError(f"discovery document at {url} is not an object")
        return doc
    raise AssertionError("unreachable")  # pragma: no cover
