"""OidcAuthMiddleware: signature, iss, aud, exp, sub, username claim, allowlist."""

from __future__ import annotations

import pytest
import respx
from joserfc.jwk import RSAKey

from .conftest import AUDIENCE, ISSUER, OIDC_ENV, make_client, make_token

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _client(**overrides: str):
    return make_client(**{**OIDC_ENV, **overrides})


def test_missing_bearer_returns_401(mock_jwks: respx.MockRouter) -> None:
    with _client() as c:
        r = c.post("/mcp/", json=_TOOLS_LIST)
        assert r.status_code == 401


def test_401_advertises_resource_metadata(mock_jwks: respx.MockRouter) -> None:
    with _client(MCP_PUBLIC_URL="https://mcp.test") as c:
        r = c.post("/mcp/", json=_TOOLS_LIST)
        assert r.status_code == 401
        www = r.headers["www-authenticate"]
        assert "resource_metadata=" in www
        assert "mcp.test/.well-known/oauth-protected-resource" in www


def test_oauth_metadata_is_public(mock_jwks: respx.MockRouter) -> None:
    with _client(MCP_PUBLIC_URL="https://mcp.test") as c:
        r = c.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        body = r.json()
        assert body["resource"] == "https://mcp.test"
        # We front the IdP as an AS facade, so we advertise *ourselves* as the
        # authorization server, not the IdP issuer.
        assert body["authorization_servers"] == ["https://mcp.test"]


def test_oauth_metadata_derives_resource_from_forwarded_headers(
    mock_jwks: respx.MockRouter,
) -> None:
    """No MCP_PUBLIC_URL: resource is built from the reverse-proxy headers,
    not the bound 0.0.0.0:port. Lets a single nginx deploy skip the env var."""
    with _client() as c:
        r = c.get(
            "/.well-known/oauth-protected-resource",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mcp.example.com",
            },
        )
        assert r.status_code == 200
        assert r.json()["resource"] == "https://mcp.example.com"


def test_www_authenticate_derives_metadata_from_forwarded_headers(
    mock_jwks: respx.MockRouter,
) -> None:
    """No MCP_PUBLIC_URL: the 401 resource_metadata URL also follows the proxy
    headers rather than baking in 0.0.0.0."""
    with _client() as c:
        r = c.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mcp.example.com",
            },
        )
        assert r.status_code == 401
        www = r.headers["www-authenticate"]
        assert (
            'resource_metadata="https://mcp.example.com'
            "/.well-known/oauth-protected-resource\"" in www
        )


def test_valid_token_passes(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    token = make_token(rsa_key)
    with _client() as c:
        r = c.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json=_INIT,
        )
        assert r.status_code == 200, r.text


def test_wrong_audience(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    token = make_token(rsa_key, aud="someone-else")
    with _client() as c:
        r = c.post("/mcp/", headers={"Authorization": f"Bearer {token}"}, json=_TOOLS_LIST)
        assert r.status_code == 401


def test_azp_satisfies_audience(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    """Keycloak often puts the client in azp rather than aud."""
    token = make_token(rsa_key, aud="account", extra={"azp": AUDIENCE})
    with _client() as c:
        r = c.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json=_INIT,
        )
        assert r.status_code == 200, r.text


def test_wrong_issuer(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    token = make_token(rsa_key, iss=ISSUER + "-evil")
    with _client() as c:
        r = c.post("/mcp/", headers={"Authorization": f"Bearer {token}"}, json=_TOOLS_LIST)
        assert r.status_code == 401


def test_expired(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    token = make_token(rsa_key, exp_offset=-10)
    with _client() as c:
        r = c.post("/mcp/", headers={"Authorization": f"Bearer {token}"}, json=_TOOLS_LIST)
        assert r.status_code == 401


def test_user_not_allowed(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    token = make_token(rsa_key, preferred_username="intruder")
    with _client() as c:
        r = c.post("/mcp/", headers={"Authorization": f"Bearer {token}"}, json=_TOOLS_LIST)
        assert r.status_code == 401
        assert "not allowed" in r.json()["reason"].lower()


@pytest.mark.parametrize(
    "token_kwargs",
    [
        pytest.param({"sub": None}, id="absent"),
        pytest.param({"sub": ""}, id="empty"),
        pytest.param({"extra": {"sub": 42}}, id="not-a-string"),
    ],
)
def test_sub_required(mock_jwks: respx.MockRouter, rsa_key: RSAKey, token_kwargs: dict) -> None:
    """A validly signed token still needs a usable ``sub``: it keys the
    outbound credential cache, so a username claim must not stand in for it
    even with the allowlist off."""
    token = make_token(rsa_key, preferred_username="alice", **token_kwargs)
    with _client() as c:
        r = c.post("/mcp/", headers={"Authorization": f"Bearer {token}"}, json=_TOOLS_LIST)
        assert r.status_code == 401
        assert "sub" in r.json()["reason"]


def test_other_signer_rejected(mock_jwks: respx.MockRouter) -> None:
    other = RSAKey.generate_key(2048, parameters={"kid": "evil", "use": "sig"})
    token = make_token(other)
    with _client() as c:
        r = c.post("/mcp/", headers={"Authorization": f"Bearer {token}"}, json=_TOOLS_LIST)
        assert r.status_code == 401


def test_custom_username_claim(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    token = make_token(rsa_key, extra={"email": "alice@example.com"})
    with _client(
        MCP_OIDC_USERNAME_CLAIM="email", MCP_OIDC_ALLOWED_USERS="alice@example.com"
    ) as c:
        r = c.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json=_INIT,
        )
        assert r.status_code == 200, r.text


def test_audience_optional(mock_jwks: respx.MockRouter, rsa_key: RSAKey) -> None:
    """When MCP_OIDC_AUDIENCE is empty, the aud claim is not validated."""
    token = make_token(rsa_key, aud="anything-goes")
    with _client(MCP_OIDC_AUDIENCE="") as c:
        r = c.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json=_INIT,
        )
        assert r.status_code == 200, r.text
