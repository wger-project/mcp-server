"""MCP_AUTH=wger_oidc: bearer gating, pass-through, allowlist, discovery.

The mode wger 2.7+ deployments run: wger issues the access token, this server
carries it through unchanged. So most of what the ``oidc`` suite asserts —
signatures, issuer, audience — has no counterpart here on purpose. What is left
to check is that the token *is* required, that it reaches wger untouched, and
that the two things this server still decides locally (the allowlist and what
the discovery documents advertise) come out right.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from pydantic import ValidationError

from wger_mcp.api_client import build_api_client
from wger_mcp.auth.exchange import WgerTokenError, WgerTokenProvider
from wger_mcp.auth.identity import Identity, reset_identity, set_identity
from wger_mcp.auth.wger_oidc import UsernameResolver, token_fingerprint
from wger_mcp.config import AuthStrategy, Settings, Transport, load_settings

from .conftest import (
    WGER_AUTHORIZE,
    WGER_BASE,
    WGER_OIDC_ENV,
    WGER_USERPROFILE,
    make_client,
)

TOKEN = "wger-opaque-access-token"
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _client(**overrides: str):
    return make_client(**{**WGER_OIDC_ENV, **overrides})


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"wger_base_url": WGER_BASE, "mcp_auth": "wger_oidc"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------- config ----------


def test_needs_nothing_but_the_wger_url() -> None:
    """The point of the mode: no client id, no secret, no audience, no issuer.
    If this ever starts requiring one, the deployment story has regressed."""
    s = _settings()
    assert s.mcp_auth is AuthStrategy.wger_oidc
    assert (s.oidc_client_id, s.oidc_client_secret, s.wger_oidc_audience) == (None, None, None)
    assert s.mcp_wger_scopes == ["openid", "api:read", "api:write"]


def test_scopes_accept_a_space_separated_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth spells a scope list with spaces, so MCP_WGER_SCOPES has to as well."""
    monkeypatch.setenv("MCP_AUTH", "wger_oidc")
    monkeypatch.setenv("MCP_WGER_SCOPES", "openid api:read")
    assert load_settings(env_file=None).mcp_wger_scopes == ["openid", "api:read"]


def test_scopes_accept_a_comma_separated_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH", "wger_oidc")
    monkeypatch.setenv("MCP_WGER_SCOPES", "openid, api:read")
    assert load_settings(env_file=None).mcp_wger_scopes == ["openid", "api:read"]


def test_empty_scopes_are_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(mcp_wger_scopes=[])
    assert "MCP_WGER_SCOPES" in str(exc.value)


def test_stdio_still_refuses_an_inbound_strategy() -> None:
    """Under stdio the client owns both ends of the pipe; there is no inbound
    request to authenticate, so choosing a strategy is a misconfiguration."""
    with pytest.raises(ValidationError) as exc:
        _settings(mcp_transport=Transport.stdio, wger_dev_token="key")
    assert "MCP_AUTH=wger_oidc" in str(exc.value)


# ---------- request gating ----------


def test_missing_bearer_returns_401(mock_wger_oidc: respx.MockRouter) -> None:
    with _client() as c:
        r = c.post("/mcp/", json=_TOOLS_LIST)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer ")


def test_401_advertises_resource_metadata(mock_wger_oidc: respx.MockRouter) -> None:
    with _client(MCP_PUBLIC_URL="https://mcp.test") as c:
        www = c.post("/mcp/", json=_TOOLS_LIST).headers["www-authenticate"]
        assert 'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource"' in www


def test_non_bearer_scheme_returns_401(mock_wger_oidc: respx.MockRouter) -> None:
    with _client() as c:
        r = c.post("/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Token {TOKEN}"})
        assert r.status_code == 401


def test_empty_bearer_returns_401(mock_wger_oidc: respx.MockRouter) -> None:
    """'Bearer ' with nothing after it parses as a bearer but is not one."""
    with _client() as c:
        r = c.post("/mcp/", json=_TOOLS_LIST, headers={"Authorization": "Bearer   "})
        assert r.status_code == 401


def test_any_bearer_is_accepted_without_an_allowlist(mock_wger_oidc: respx.MockRouter) -> None:
    """Nothing is validated here: the token is opaque, and wger checks it on the
    call itself. Not even a userprofile lookup happens (the route is unmocked,
    so a request to it would fail the test)."""
    with _client() as c:
        r = c.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={"Authorization": f"Bearer {TOKEN}", **_MCP_HEADERS},
        )
        assert r.status_code != 401


def test_bypass_paths_stay_public(mock_wger_oidc: respx.MockRouter) -> None:
    with _client() as c:
        assert c.get("/health").status_code == 200
        assert c.get("/.well-known/oauth-protected-resource").status_code == 200
        assert c.get("/.well-known/oauth-authorization-server").status_code == 200
        assert c.get("/authorize?client_id=x", follow_redirects=False).status_code == 302


# ---------- the allowlist ----------


def test_allowlist_lets_the_named_user_through(mock_wger_oidc: respx.MockRouter) -> None:
    mock_wger_oidc.get(WGER_USERPROFILE).respond(json={"username": "alice"})
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        r = c.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={"Authorization": f"Bearer {TOKEN}", **_MCP_HEADERS},
        )
        assert r.status_code != 401


def test_allowlist_rejects_everyone_else(mock_wger_oidc: respx.MockRouter) -> None:
    mock_wger_oidc.get(WGER_USERPROFILE).respond(json={"username": "mallory"})
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        r = c.post("/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 401
        assert r.json()["reason"] == "user not allowed"


def test_a_token_wger_rejects_is_reported_as_such(mock_wger_oidc: respx.MockRouter) -> None:
    mock_wger_oidc.get(WGER_USERPROFILE).respond(401, json={"detail": "Invalid token."})
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        r = c.post("/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 401
        assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_a_grant_without_api_read_is_a_403(mock_wger_oidc: respx.MockRouter) -> None:
    """403 rather than 401 on purpose: re-running the OAuth flow returns the same
    token, so the client has to be told the *grant* is short, not the token."""
    mock_wger_oidc.get(WGER_USERPROFILE).respond(403, json={"detail": "missing scope"})
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        r = c.post("/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 403
        assert 'error="insufficient_scope"' in r.headers["www-authenticate"]
        assert "api:read" in r.json()["reason"]


def test_an_unreachable_wger_does_not_let_the_request_through(
    mock_wger_oidc: respx.MockRouter,
) -> None:
    mock_wger_oidc.get(WGER_USERPROFILE).mock(side_effect=httpx.ConnectError("down"))
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        r = c.post("/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 401


def test_the_username_is_looked_up_once_per_token(mock_wger_oidc: respx.MockRouter) -> None:
    """The lookup is a round trip on wger's slowest authentication path, so it
    must not happen per request."""
    route = mock_wger_oidc.get(WGER_USERPROFILE).respond(json={"username": "alice"})
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        for _ in range(3):
            c.post(
                "/mcp/",
                json=_TOOLS_LIST,
                headers={"Authorization": f"Bearer {TOKEN}", **_MCP_HEADERS},
            )
    assert route.call_count == 1


def test_a_second_token_is_looked_up_separately(mock_wger_oidc: respx.MockRouter) -> None:
    route = mock_wger_oidc.get(WGER_USERPROFILE).respond(json={"username": "alice"})
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        for token in (TOKEN, TOKEN + "-other"):
            c.post(
                "/mcp/",
                json=_TOOLS_LIST,
                headers={"Authorization": f"Bearer {token}", **_MCP_HEADERS},
            )
    assert route.call_count == 2


def test_a_failed_lookup_is_not_cached(mock_wger_oidc: respx.MockRouter) -> None:
    """Caching a failure would keep a user out for as long as the process lives,
    long after the outage that caused it."""
    route = mock_wger_oidc.get(WGER_USERPROFILE)
    route.mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(200, json={"username": "alice"}),
        ]
    )
    with _client(MCP_OIDC_ALLOWED_USERS="alice") as c:
        assert c.post(
            "/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code == 401
        assert c.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={"Authorization": f"Bearer {TOKEN}", **_MCP_HEADERS},
        ).status_code != 401


# ---------- the resolver on its own ----------


@pytest.mark.asyncio
async def test_concurrent_lookups_share_one_request() -> None:
    """The fan-out tools issue many calls at once; each one asking wger who the
    caller is would multiply that by two."""
    resolver = UsernameResolver(WGER_BASE)
    with respx.mock() as router:
        route = router.get(WGER_USERPROFILE).respond(json={"username": "alice"})
        results = await asyncio.gather(
            *(resolver.username_for(TOKEN, "fp") for _ in range(5))
        )
    assert results == ["alice"] * 5
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_an_answer_that_names_nobody_is_not_cached() -> None:
    """Caching it would lock the user out for the life of the process over one
    malformed response."""
    resolver = UsernameResolver(WGER_BASE)
    with respx.mock() as router:
        route = router.get(WGER_USERPROFILE)
        route.mock(
            side_effect=[
                httpx.Response(200, json={}),
                httpx.Response(200, json={"username": "alice"}),
            ]
        )
        assert await resolver.username_for(TOKEN, "fp") is None
        assert await resolver.username_for(TOKEN, "fp") == "alice"
    assert route.call_count == 2


# ---------- the fingerprint ----------


def test_the_fingerprint_never_contains_the_token() -> None:
    fp = token_fingerprint(TOKEN)
    assert TOKEN not in fp
    assert fp == token_fingerprint(TOKEN) != token_fingerprint(TOKEN + "!")


# ---------- pass-through ----------


@pytest.mark.asyncio
async def test_the_outbound_call_carries_the_inbound_token() -> None:
    provider = WgerTokenProvider(pass_through=True)
    settings = _settings()
    api = build_api_client(settings, provider)
    ctx = set_identity(
        Identity(subject="fp", inbound_token=TOKEN, strategy="wger_oidc")
    )
    try:
        with respx.mock() as router:
            route = router.get(f"{WGER_BASE}/api/v2/version/").respond(json={})
            await api.get_async_httpx_client().get("/api/v2/version/")
    finally:
        reset_identity(ctx)
        await api.get_async_httpx_client().aclose()
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_pass_through_without_an_identity_is_an_error() -> None:
    """Belt and braces: the middleware always binds one, and a bug that skipped
    it must not fall back to calling wger unauthenticated."""
    with pytest.raises(WgerTokenError):
        await WgerTokenProvider(pass_through=True).authorization_header()


# ---------- what the discovery documents say ----------


def test_protected_resource_metadata_names_the_scopes(
    mock_wger_oidc: respx.MockRouter,
) -> None:
    with _client(MCP_PUBLIC_URL="https://mcp.test") as c:
        body = c.get("/.well-known/oauth-protected-resource").json()
        assert body["resource"] == "https://mcp.test"
        assert body["authorization_servers"] == ["https://mcp.test"]
        assert body["scopes_supported"] == ["openid", "api:read", "api:write"]


def test_the_facade_can_be_turned_off(mock_wger_oidc: respx.MockRouter) -> None:
    """MCP_AS_FACADE=false points clients at wger itself — right for clients that
    follow the pointer, and then this origin serves no OAuth endpoints at all."""
    with _client(MCP_PUBLIC_URL="https://mcp.test", MCP_AS_FACADE="false") as c:
        body = c.get("/.well-known/oauth-protected-resource").json()
        assert body["authorization_servers"] == [WGER_BASE]
        assert c.get("/.well-known/oauth-authorization-server").status_code == 404
        assert c.get("/authorize", follow_redirects=False).status_code == 401


def test_authorize_points_at_wger(mock_wger_oidc: respx.MockRouter) -> None:
    """Discovered, not hard-coded: the endpoint comes from wger's own document."""
    with _client() as c:
        r = c.get("/authorize?response_type=code&client_id=x", follow_redirects=False)
        assert r.headers["location"].startswith(WGER_AUTHORIZE + "?")
