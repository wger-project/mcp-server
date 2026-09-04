"""JwksCache: what reaches the IdP, and what a key rotation still costs.

The forced refetch is reachable by an unauthenticated request — anyone can
present a token — so these pin down which rejected tokens are allowed to turn
into traffic, and that rotation keeps working anyway.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import respx
from httpx import Response
from joserfc.jwk import RSAKey

from wger_mcp.auth.oidc import JwksCache

from .conftest import JWKS_URI, OIDC_ENV, make_client, make_token

_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
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


def _client(**overrides: str):
    return make_client(**{**OIDC_ENV, **overrides})


def _jwks_of(*keys: RSAKey) -> dict[str, Any]:
    out = []
    for key in keys:
        pub = key.as_dict(private=False)
        pub.setdefault("alg", "RS256")
        out.append(pub)
    return {"keys": out}


def _auth(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }


def test_bad_signature_does_not_refetch_the_jwks(
    mock_jwks: respx.MockRouter, rsa_key: RSAKey
) -> None:
    """A token signed by a stranger, but naming a key id we hold, is simply
    wrong — refetching cannot change that, so it must not reach the IdP."""
    impostor = RSAKey.generate_key(2048, parameters={"kid": rsa_key.kid, "use": "sig"})
    token = make_token(impostor)
    with _client() as c:
        for _ in range(3):
            r = c.post("/mcp/", headers=_auth(token), json=_TOOLS_LIST)
            assert r.status_code == 401
    # One fetch to populate the cache, and nothing after it.
    assert len(mock_jwks.calls) == 1


def test_garbage_token_does_not_refetch_the_jwks(
    mock_jwks: respx.MockRouter, rsa_key: RSAKey
) -> None:
    """The cheapest thing an attacker can send must be the cheapest to reject."""
    with _client() as c:
        assert c.post("/mcp/", headers=_auth(make_token(rsa_key)), json=_INIT).status_code == 200
        for _ in range(5):
            r = c.post("/mcp/", headers=_auth("not.a.jwt"), json=_TOOLS_LIST)
            assert r.status_code == 401
    assert len(mock_jwks.calls) == 1


def test_unknown_key_id_refetches_only_once_within_the_window(
    mock_jwks: respx.MockRouter, rsa_key: RSAKey
) -> None:
    """An unknown kid is what a rotation looks like, so it earns one refetch —
    but the sender is unauthenticated, so it earns only one per window."""
    stranger = RSAKey.generate_key(2048, parameters={"kid": "never-published", "use": "sig"})
    token = make_token(stranger)
    with _client() as c:
        for _ in range(4):
            r = c.post("/mcp/", headers=_auth(token), json=_TOOLS_LIST)
            assert r.status_code == 401
    # The initial fetch plus a single forced one, not one per request.
    assert len(mock_jwks.calls) == 2


def test_rotated_key_is_picked_up_without_waiting_for_the_ttl(rsa_key: RSAKey) -> None:
    """The refetch still does its job: a token signed with a key published
    after the cache was filled validates on the retry."""
    rotated = RSAKey.generate_key(2048, parameters={"kid": "rotated-2", "use": "sig"})
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URI)
        route.side_effect = [
            Response(200, json=_jwks_of(rsa_key)),
            Response(200, json=_jwks_of(rsa_key, rotated)),
        ]
        with _client() as c:
            # Fills the cache with the pre-rotation key set.
            filled = c.post("/mcp/", headers=_auth(make_token(rsa_key)), json=_INIT)
            assert filled.status_code == 200, filled.text
            r = c.post("/mcp/", headers=_auth(make_token(rotated)), json=_INIT)
    assert r.status_code == 200, r.text
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_misses_make_one_request(rsa_key: RSAKey) -> None:
    """Ten requests arriving on a cold cache are one fetch, not ten."""
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URI).respond(json=_jwks_of(rsa_key))
        cache = JwksCache(JWKS_URI, ttl_seconds=3600)
        await asyncio.gather(*[cache.get() for _ in range(10)])
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_forced_refetch_is_allowed_again_after_the_window(rsa_key: RSAKey) -> None:
    """The floor delays a forced refetch, it does not disable it."""
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JWKS_URI).respond(json=_jwks_of(rsa_key))
        cache = JwksCache(JWKS_URI, ttl_seconds=3600, min_forced_interval=0.0)
        await cache.get()
        await cache.get(force=True)
    assert route.call_count == 2
