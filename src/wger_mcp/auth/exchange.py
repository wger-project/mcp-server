"""Outbound credential: what goes into ``Authorization`` on a wger API call.

Three shapes, one per inbound strategy, all behind :class:`WgerTokenProvider`:

- ``wger_oidc`` — **pass-through**. wger issued the caller's token and accepts
  it on its own API, so it is forwarded unchanged. Nothing to exchange, nothing
  to cache (ADR 0005).
- ``static_token`` / ``none`` — a personal wger API key, the same for everyone.
- ``oidc`` — the two-hop exchange below, for an external IdP (ADR 0001).

Two hops (see ADR 0001):

1. **RFC 8693 token-exchange** against the SSO IdP — the MCP is a confidential
   client and swaps the caller's inbound token for one whose audience is
   wger's OIDC client.
2. **allauth headless ``provider/token``** against wger — wger logs the user in
   from that token and returns a native wger JWT.

The wger access token (~5 min) is cached in memory per IdP ``sub``; on expiry
we re-run both hops rather than storing wger's rotating refresh tokens.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from typing import Any

import httpx

from .identity import Identity, current_identity

log = logging.getLogger(__name__)

_GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_TT_ACCESS = "urn:ietf:params:oauth:token-type:access_token"
_FALLBACK_TTL = 240.0  # seconds; below wger's 5-min access lifetime
_EXPIRY_SKEW = 30.0  # refresh a bit before actual expiry


class WgerTokenError(RuntimeError):
    """Raised when an outbound wger credential cannot be obtained."""


def _jwt_exp(token: str) -> float | None:
    """Best-effort read of a JWT's ``exp`` (no signature check — cache TTL only)."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


class TokenExchanger:
    """Exchanges SSO (OIDC) tokens for wger JWTs, with a per-user cache."""

    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        wger_audience: str,
        provider_token_url: str,
        provider: str,
        timeout: float = 15.0,
    ) -> None:
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._wger_audience = wger_audience
        self._provider_token_url = provider_token_url
        self._provider = provider
        self._client = httpx.AsyncClient(timeout=timeout)
        # subject -> (wger_access_token, expires_at_epoch)
        self._cache: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _lock_for(self, subject: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(subject)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[subject] = lock
            return lock

    async def wger_token_for(self, identity: Identity) -> str:
        if not identity.inbound_token:
            raise WgerTokenError("no inbound SSO token to exchange")
        now = time.time()
        cached = self._cache.get(identity.subject)
        if cached and cached[1] - _EXPIRY_SKEW > now:
            return cached[0]

        lock = await self._lock_for(identity.subject)
        async with lock:
            # Re-check: another coroutine may have refreshed while we waited.
            cached = self._cache.get(identity.subject)
            now = time.time()
            if cached and cached[1] - _EXPIRY_SKEW > now:
                return cached[0]
            token = await self._exchange(identity.inbound_token)
            exp = _jwt_exp(token) or (time.time() + _FALLBACK_TTL)
            self._cache[identity.subject] = (token, exp)
            return token

    async def _exchange(self, inbound_token: str) -> str:
        kc = await self._idp_exchange(inbound_token)
        return await self._wger_login(kc)

    async def _idp_exchange(self, inbound_token: str) -> dict[str, Any]:
        """RFC 8693: swap the inbound token for one whose ``aud`` is wger's client.

        We request an *access_token*: the IdP audiences it at the requested
        ``audience`` (wger), which is what allauth validates. (A requested
        *id_token*, by contrast, is audienced at the requesting client and is
        rejected by wger.)
        """
        data = {
            "grant_type": _GRANT_TOKEN_EXCHANGE,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "subject_token": inbound_token,
            "subject_token_type": _TT_ACCESS,
            "requested_token_type": _TT_ACCESS,
            "audience": self._wger_audience,
        }
        try:
            resp = await self._client.post(self._token_endpoint, data=data)
        except httpx.HTTPError as exc:
            raise WgerTokenError(f"IdP token-exchange unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise WgerTokenError(
                f"IdP token-exchange failed ({resp.status_code}): {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise WgerTokenError("IdP token-exchange returned non-JSON") from exc

    async def _wger_login(self, kc_tokens: dict[str, Any]) -> str:
        """allauth headless provider/token: trade the exchanged token for a wger JWT.

        allauth's ``openid_connect.verify_token`` reads ``token.id_token`` and
        validates its signature/issuer/audience. The exchanged access_token
        (``aud=wger``) satisfies that; we send it under the ``id_token`` key.
        """
        wger_aud_token = kc_tokens.get("access_token") or kc_tokens.get("id_token")
        if not wger_aud_token:
            raise WgerTokenError("IdP exchange returned no usable token")
        token_obj: dict[str, Any] = {
            "client_id": self._wger_audience,
            "id_token": wger_aud_token,
            "access_token": wger_aud_token,
        }

        body = {"provider": self._provider, "process": "login", "token": token_obj}
        try:
            resp = await self._client.post(self._provider_token_url, json=body)
        except httpx.HTTPError as exc:
            raise WgerTokenError(f"wger provider/token unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise WgerTokenError(
                f"wger provider/token failed ({resp.status_code}): {resp.text[:300]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WgerTokenError("wger provider/token returned non-JSON") from exc

        jwt_token = _extract_wger_jwt(payload)
        if not jwt_token:
            raise WgerTokenError(
                f"could not find a wger JWT in provider/token response: {str(payload)[:300]}"
            )
        return jwt_token


def _extract_wger_jwt(payload: dict[str, Any]) -> str | None:
    """Pull the wger access JWT from an allauth headless response.

    allauth's JWT token strategy returns it under ``meta.access_token``; we also
    accept a few sensible fallbacks across allauth versions.
    """
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        for key in ("access_token", "access", "token"):
            val = meta.get(key)
            if isinstance(val, str) and val:
                return val
    for key in ("access_token", "access", "token"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return None


class WgerTokenProvider:
    """Supplies the ``Authorization`` header value for an outbound wger call.

    - ``pass_through`` mode (``MCP_AUTH=wger_oidc``): the caller's own wger
      token, forwarded verbatim → ``Bearer <token>``.
    - ``exchanger`` mode (``MCP_AUTH=oidc``): per-request, exchange the caller's
      SSO token for a wger JWT → ``Bearer <jwt>``.
    - ``dev`` mode (``MCP_AUTH=none`` / ``static_token``): a static personal DRF
      key → ``Token <key>``.

    One class rather than three so the single call site in ``api_client`` and
    the lifespan teardown do not have to know which mode is running.
    """

    def __init__(
        self,
        *,
        exchanger: TokenExchanger | None = None,
        dev_token: str | None = None,
        pass_through: bool = False,
    ) -> None:
        if not pass_through and exchanger is None and not dev_token:
            raise ValueError(
                "WgerTokenProvider needs pass_through, an exchanger or a dev_token"
            )
        self._exchanger = exchanger
        self._dev_token = dev_token
        self._pass_through = pass_through

    async def authorization_header(self) -> str:
        if self._pass_through:
            identity = current_identity()
            if identity is None or not identity.inbound_token:
                raise WgerTokenError("no caller identity bound to this request")
            return f"Bearer {identity.inbound_token}"
        if self._exchanger is None:
            return f"Token {self._dev_token}"
        identity = current_identity()
        if identity is None:
            raise WgerTokenError("no caller identity bound to this request")
        token = await self._exchanger.wger_token_for(identity)
        return f"Bearer {token}"

    async def aclose(self) -> None:
        if self._exchanger is not None:
            await self._exchanger.aclose()
