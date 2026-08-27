"""AuthorizationServerFacade: AS metadata, /authorize, /token, /register.

The facade lets a client that treats this origin as the OAuth authorization
server (e.g. claude.ai) drive the flow against us: front-channel /authorize
bounces the browser to the provider, the back-channel endpoints are
reverse-proxied to it.

Two providers, two halves below: an external IdP, where the query is passed
through verbatim, and wger itself, where the API scopes are added because no
generic MCP client knows about them.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import respx

from .conftest import (
    AUTHORIZATION_ENDPOINT,
    OIDC_ENV,
    TOKEN_ENDPOINT,
    WGER_AUTHORIZE,
    WGER_OIDC_ENV,
    WGER_REGISTER,
    WGER_TOKEN,
    make_client,
)

SCOPES = ["openid", "api:read", "api:write"]


def _client(**overrides: str):
    return make_client(**{**OIDC_ENV, **overrides})


def _wger_client(**overrides: str):
    return make_client(**{**WGER_OIDC_ENV, **overrides})


def _authorize_scope(client, query: str) -> list[str]:
    r = client.get(f"/authorize?{query}", follow_redirects=False)
    assert r.status_code == 302
    scope = parse_qs(urlparse(r.headers["location"]).query).get("scope")
    return scope[0].split() if scope else []


def test_as_metadata_advertises_facade_endpoints(mock_jwks: respx.MockRouter) -> None:
    with _client(MCP_PUBLIC_URL="https://mcp.test") as c:
        r = c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        body = r.json()
        assert body["issuer"] == "https://mcp.test"
        assert body["authorization_endpoint"] == "https://mcp.test/authorize"
        assert body["token_endpoint"] == "https://mcp.test/token"
        assert body["code_challenge_methods_supported"] == ["S256"]


def test_as_metadata_derives_origin_from_forwarded_headers(
    mock_jwks: respx.MockRouter,
) -> None:
    """No MCP_PUBLIC_URL: endpoints follow the reverse-proxy/tunnel host."""
    with _client() as c:
        r = c.get(
            "/.well-known/oauth-authorization-server",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "mcp.example.com"},
        )
        assert r.status_code == 200
        assert r.json()["authorization_endpoint"] == "https://mcp.example.com/authorize"


def test_authorize_redirects_to_idp_preserving_query(mock_jwks: respx.MockRouter) -> None:
    with _client() as c:
        q = (
            "response_type=code&client_id=wger-mcp"
            "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            "&code_challenge=abc123&code_challenge_method=S256&state=xyz"
        )
        r = c.get(f"/authorize?{q}", follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith(AUTHORIZATION_ENDPOINT + "?")
        assert "client_id=wger-mcp" in loc
        assert "code_challenge=abc123" in loc
        assert "state=xyz" in loc
        # redirect_uri preserved (urlencoded) so the IdP validates the real one
        assert "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback" in loc


def test_authorize_is_public(mock_jwks: respx.MockRouter) -> None:
    """No bearer token needed to start the flow (302, not 401)."""
    with _client() as c:
        r = c.get(
            "/authorize?response_type=code&client_id=wger-mcp",
            follow_redirects=False,
        )
        assert r.status_code == 302


def test_token_reverse_proxies_to_idp(mock_jwks: respx.MockRouter) -> None:
    route = mock_jwks.post(TOKEN_ENDPOINT).respond(
        json={"access_token": "AT", "token_type": "Bearer", "expires_in": 300}
    )
    with _client() as c:
        r = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": "the-code",
                "code_verifier": "the-verifier",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            },
        )
        assert r.status_code == 200
        assert r.json()["access_token"] == "AT"
        assert route.called
        sent = route.calls.last.request.content.decode()
        assert "grant_type=authorization_code" in sent
        assert "code_verifier=the-verifier" in sent


def test_facade_paths_are_overridable(mock_jwks: respx.MockRouter) -> None:
    """OAUTH_AUTHORIZE_PATH / OAUTH_TOKEN_PATH override the default root paths,
    in both the AS metadata and the live (auth-bypassed) routes."""
    with _client(
        MCP_PUBLIC_URL="https://mcp.test",
        OAUTH_AUTHORIZE_PATH="/oauth/authorize",
        OAUTH_TOKEN_PATH="/oauth/token",
    ) as c:
        meta = c.get("/.well-known/oauth-authorization-server").json()
        assert meta["authorization_endpoint"] == "https://mcp.test/oauth/authorize"
        assert meta["token_endpoint"] == "https://mcp.test/oauth/token"
        # the overridden authorize path is served and public (302, not 401)
        r = c.get(
            "/oauth/authorize?response_type=code&client_id=wger-mcp",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # and the default root path is no longer wired → 401 (auth challenge)
        assert c.get("/authorize", follow_redirects=False).status_code == 401


# ---------- native wger OIDC ----------
#
# Same facade, pointed at wger and with one thing added: this server knows which
# scopes wger's API gates on, and a generic MCP client does not.


def test_facade_endpoints_come_from_wgers_discovery_document(
    mock_wger_oidc: respx.MockRouter,
) -> None:
    with _wger_client(MCP_PUBLIC_URL="https://mcp.test") as c:
        meta = c.get("/.well-known/oauth-authorization-server").json()
        assert meta["authorization_endpoint"] == "https://mcp.test/authorize"
        assert meta["scopes_supported"] == SCOPES
        r = c.get("/authorize?client_id=x", follow_redirects=False)
        assert r.headers["location"].startswith(WGER_AUTHORIZE + "?")


def test_token_reverse_proxies_to_wger(mock_wger_oidc: respx.MockRouter) -> None:
    route = mock_wger_oidc.post(WGER_TOKEN).respond(json={"access_token": "AT"})
    with _wger_client() as c:
        r = c.post("/token", data={"grant_type": "authorization_code", "code": "c"})
        assert r.status_code == 200 and r.json()["access_token"] == "AT"
        assert "code=c" in route.calls.last.request.content.decode()


def test_missing_api_scopes_are_added_to_the_authorization_request(
    mock_wger_oidc: respx.MockRouter,
) -> None:
    """A client that asks for `openid` alone would be refused at wger — allauth
    requires the requested scopes to be a subset of the client's — or, worse,
    would get a token that fails on every API call. Neither is diagnosable from
    the client, so the facade adds what it knows is needed."""
    with _wger_client() as c:
        assert _authorize_scope(c, "client_id=x&scope=openid") == SCOPES


def test_scopes_are_added_when_the_client_asks_for_none(
    mock_wger_oidc: respx.MockRouter,
) -> None:
    with _wger_client() as c:
        assert _authorize_scope(c, "client_id=x") == SCOPES


def test_scopes_the_client_asked_for_are_kept(mock_wger_oidc: respx.MockRouter) -> None:
    """Adding, never replacing: a client that wants `email` too still gets it."""
    with _wger_client() as c:
        assert _authorize_scope(c, "client_id=x&scope=openid+email") == [
            "openid", "email", "api:read", "api:write"
        ]


def test_the_rest_of_the_authorization_query_survives(
    mock_wger_oidc: respx.MockRouter,
) -> None:
    """The scope rewrite re-encodes the query, so everything PKCE and the redirect
    depend on has to come out the other side unchanged."""
    with _wger_client() as c:
        r = c.get(
            "/authorize?response_type=code&client_id=x"
            "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            "&code_challenge=abc123&code_challenge_method=S256&state=xyz",
            follow_redirects=False,
        )
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["redirect_uri"] == ["https://claude.ai/api/mcp/auth_callback"]
        assert q["code_challenge"] == ["abc123"]
        assert q["code_challenge_method"] == ["S256"]
        assert q["state"] == ["xyz"]
        assert q["response_type"] == ["code"]


def test_an_external_idp_gets_its_query_untouched(mock_jwks: respx.MockRouter) -> None:
    """Only wger_oidc knows the scope names; with an external IdP the deployment
    configured the client and this server has no business editing the request."""
    with _client() as c:
        assert _authorize_scope(c, "client_id=x&scope=openid") == ["openid"]


# ---------- dynamic client registration ----------


def test_registration_is_not_offered_when_wger_has_dcr_off(
    mock_wger_oidc: respx.MockRouter,
) -> None:
    """wger publishes a registration_endpoint exactly when DCR is on, so there is
    nothing to configure here — and no route to a 404 that a client would read
    as a transient failure."""
    with _wger_client(MCP_PUBLIC_URL="https://mcp.test") as c:
        assert "registration_endpoint" not in c.get(
            "/.well-known/oauth-authorization-server"
        ).json()
        assert c.post("/register", json={}).status_code == 404


def test_registration_is_advertised_and_proxied_when_dcr_is_on(
    mock_wger_oidc_dcr: respx.MockRouter,
) -> None:
    route = mock_wger_oidc_dcr.post(WGER_REGISTER).respond(
        201, json={"client_id": "generated", "client_secret": "s"}
    )
    with _wger_client(MCP_PUBLIC_URL="https://mcp.test") as c:
        meta = c.get("/.well-known/oauth-authorization-server").json()
        assert meta["registration_endpoint"] == "https://mcp.test/register"
        r = c.post(
            "/register",
            json={"client_name": "claude", "redirect_uris": ["https://claude.ai/cb"]},
        )
        assert r.status_code == 201 and r.json()["client_id"] == "generated"
    sent = json.loads(route.calls.last.request.content)
    assert sent["redirect_uris"] == ["https://claude.ai/cb"]
    # The whole reason to proxy rather than advertise wger's endpoint directly:
    # a client registered with "openid" alone can never reach the API.
    assert sent["scope"].split() == SCOPES


def test_registration_keeps_what_the_client_asked_for(
    mock_wger_oidc_dcr: respx.MockRouter,
) -> None:
    route = mock_wger_oidc_dcr.post(WGER_REGISTER).respond(201, json={})
    with _wger_client() as c:
        c.post("/register", json={"scope": "openid email", "redirect_uris": ["u"]})
    assert json.loads(route.calls.last.request.content)["scope"].split() == [
        "openid", "email", "api:read", "api:write"
    ]


def test_a_registration_body_that_is_not_json_is_forwarded_untouched(
    mock_wger_oidc_dcr: respx.MockRouter,
) -> None:
    """wger's 400 says what is wrong with it; a guess made here would not."""
    route = mock_wger_oidc_dcr.post(WGER_REGISTER).respond(400, json={"error": "x"})
    with _wger_client() as c:
        r = c.post("/register", content=b"not json", headers={"content-type": "text/plain"})
        assert r.status_code == 400
    assert route.calls.last.request.content == b"not json"


def test_registration_forwards_an_initial_access_token(
    mock_wger_oidc_dcr: respx.MockRouter,
) -> None:
    """allauth can require one; the facade is not the place it gets lost."""
    route = mock_wger_oidc_dcr.post(WGER_REGISTER).respond(201, json={})
    with _wger_client() as c:
        c.post(
            "/register",
            json={"redirect_uris": ["u"]},
            headers={"Authorization": "Bearer initial-access-token"},
        )
    assert route.calls.last.request.headers["authorization"] == "Bearer initial-access-token"
