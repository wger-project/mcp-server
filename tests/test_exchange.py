"""Token exchange: OIDC RFC 8693 -> wger provider/token -> wger JWT, cached."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
import respx

from wger_mcp.auth.exchange import TokenExchanger, WgerTokenError, WgerTokenProvider
from wger_mcp.auth.identity import Identity, reset_identity, set_identity

TOKEN_ENDPOINT = "https://idp.test/realms/test/protocol/openid-connect/token"
PROVIDER_TOKEN_URL = "https://wger.test/allauth/app/v1/auth/provider/token"


def _fake_jwt(exp_offset: int = 3600) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + exp_offset}).encode()
    ).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig"


def _exchanger() -> TokenExchanger:
    return TokenExchanger(
        token_endpoint=TOKEN_ENDPOINT,
        client_id="wger-mcp",
        client_secret="shh",
        wger_audience="wger",
        provider_token_url=PROVIDER_TOKEN_URL,
        provider="openid_connect",
    )


@pytest.mark.asyncio
async def test_exchange_chain_and_cache() -> None:
    wger_jwt = _fake_jwt()
    ex = _exchanger()
    try:
        with respx.mock() as router:
            kc = router.post(TOKEN_ENDPOINT).respond(
                json={"access_token": "kc-access", "id_token": "kc-id"}
            )
            wg = router.post(PROVIDER_TOKEN_URL).respond(json={"meta": {"access_token": wger_jwt}})

            identity = Identity(subject="uuid-alice", inbound_token="inbound-kc-token")
            first = await ex.wger_token_for(identity)
            assert first == wger_jwt
            assert kc.call_count == 1 and wg.call_count == 1

            # forwarded the inbound token, requested wger audience, asked for id_token
            form = dict(
                p.split("=", 1) for p in bytes(kc.calls.last.request.content).decode().split("&")
            )
            assert form["subject_token"] == "inbound-kc-token"
            assert form["audience"] == "wger"
            assert form["requested_token_type"].endswith("access_token")

            # the wger-audienced access_token is sent under token.id_token
            wg_body = json.loads(bytes(wg.calls.last.request.content).decode())
            assert wg_body["token"]["id_token"] == "kc-access"
            assert wg_body["provider"] == "openid_connect"

            # second call is served from cache (no new HTTP)
            second = await ex.wger_token_for(identity)
            assert second == wger_jwt
            assert kc.call_count == 1 and wg.call_count == 1
    finally:
        await ex.aclose()


@pytest.mark.asyncio
async def test_cache_is_per_subject() -> None:
    """Each subject exchanges its own inbound token and gets its own wger JWT."""
    ex = _exchanger()
    try:
        with respx.mock() as router:
            kc = router.post(TOKEN_ENDPOINT).respond(json={"access_token": "kc-access"})
            wg = router.post(PROVIDER_TOKEN_URL)
            wg.side_effect = [
                httpx.Response(200, json={"meta": {"access_token": _fake_jwt(3600)}}),
                httpx.Response(200, json={"meta": {"access_token": _fake_jwt(7200)}}),
            ]

            alice = await ex.wger_token_for(
                Identity(subject="uuid-alice", inbound_token="alice-inbound")
            )
            bob = await ex.wger_token_for(Identity(subject="uuid-bob", inbound_token="bob-inbound"))
            assert alice != bob
            assert kc.call_count == 2 and wg.call_count == 2
            forwarded = [
                dict(p.split("=", 1) for p in bytes(c.request.content).decode().split("&"))[
                    "subject_token"
                ]
                for c in kc.calls
            ]
            assert forwarded == ["alice-inbound", "bob-inbound"]

            # each subject keeps hitting its own cache entry
            assert (
                await ex.wger_token_for(Identity(subject="uuid-alice", inbound_token="x")) == alice
            )
            assert await ex.wger_token_for(Identity(subject="uuid-bob", inbound_token="y")) == bob
            assert kc.call_count == 2 and wg.call_count == 2
    finally:
        await ex.aclose()


@pytest.mark.asyncio
async def test_expired_cache_re_exchanges() -> None:
    ex = _exchanger()
    try:
        with respx.mock() as router:
            router.post(TOKEN_ENDPOINT).respond(json={"access_token": "kc"})
            wg = router.post(PROVIDER_TOKEN_URL).respond(
                json={"meta": {"access_token": _fake_jwt(exp_offset=1)}}
            )
            identity = Identity(subject="s", inbound_token="t")
            await ex.wger_token_for(identity)
            # token already within the expiry skew -> next call re-exchanges
            await ex.wger_token_for(identity)
            assert wg.call_count == 2
    finally:
        await ex.aclose()


@pytest.mark.asyncio
async def test_provider_token_failure_surfaces() -> None:
    ex = _exchanger()
    try:
        with respx.mock() as router:
            router.post(TOKEN_ENDPOINT).respond(json={"access_token": "kc"})
            router.post(PROVIDER_TOKEN_URL).respond(status_code=400, json={"error": "bad"})
            with pytest.raises(WgerTokenError):
                await ex.wger_token_for(Identity(subject="s", inbound_token="t"))
    finally:
        await ex.aclose()


@pytest.mark.asyncio
async def test_provider_dev_mode_returns_token_header() -> None:
    provider = WgerTokenProvider(dev_token="devkey")
    assert await provider.authorization_header() == "Token devkey"
    await provider.aclose()


@pytest.mark.asyncio
async def test_provider_keycloak_mode_uses_bound_identity() -> None:
    wger_jwt = _fake_jwt()
    provider = WgerTokenProvider(exchanger=_exchanger())
    ctx = set_identity(Identity(subject="s", inbound_token="t"))
    try:
        with respx.mock() as router:
            router.post(TOKEN_ENDPOINT).respond(json={"access_token": "kc"})
            router.post(PROVIDER_TOKEN_URL).respond(json={"meta": {"access_token": wger_jwt}})
            header = await provider.authorization_header()
            assert header == f"Bearer {wger_jwt}"
    finally:
        reset_identity(ctx)
        await provider.aclose()


@pytest.mark.asyncio
async def test_provider_keycloak_mode_without_identity_errors() -> None:
    provider = WgerTokenProvider(exchanger=_exchanger())
    try:
        with pytest.raises(WgerTokenError):
            await provider.authorization_header()
    finally:
        await provider.aclose()
