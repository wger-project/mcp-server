"""Inbound auth: validate an SSO (OIDC) token.

The client presents ``Authorization: Bearer <oidc-token>`` (obtained via
MCP-native OAuth with the IdP as the authorization server, or out-of-band).
The token is validated against the IdP's JWKS; the raw token is then carried
on the request :class:`Identity` so the outbound layer can exchange it for a
wger credential (see ``exchange.py``). Provider-agnostic — any OIDC IdP.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .base import is_bypass_path, reply_unauthorized
from .identity import Identity, reset_identity, set_identity
from .oauth import WELL_KNOWN_PATH, forwarded_origin

log = logging.getLogger(__name__)


# The shortest gap between two forced refetches. A forced refetch is asked for
# by an *unauthenticated* request — anyone can present a token naming a key id
# we do not hold — so without a floor a stream of such tokens is a stream of
# requests to the IdP, at whatever rate the sender likes.
_MIN_FORCED_REFETCH_INTERVAL = 60.0


class JwksCache:
    """The IdP's signing keys, refetched when ``ttl_seconds`` has passed.

    ``force`` asks for one ahead of the TTL, which is how a key rotated at the
    IdP is picked up before the cache would notice. It is rate-limited by
    :data:`_MIN_FORCED_REFETCH_INTERVAL` and, like the ordinary refresh,
    deduplicated: concurrent requests arriving on an empty or stale cache make
    one fetch between them rather than one apiece.
    """

    def __init__(
        self,
        uri: str,
        ttl_seconds: int,
        *,
        min_forced_interval: float = _MIN_FORCED_REFETCH_INTERVAL,
    ) -> None:
        self._uri = uri
        self._ttl = ttl_seconds
        self._min_forced_interval = min_forced_interval
        self._keys: KeySet | None = None
        self._fetched_at: float = 0.0
        # Of the last *forced* refetch specifically. Measuring the floor from
        # the last fetch of any kind would make an ordinary refresh — or the
        # very first one — start the window, and a key rotated moments later
        # would be answered with 401s until it ran out.
        self._forced_at: float = 0.0
        self._lock = asyncio.Lock()

    def _stale(self, force: bool) -> bool:
        if self._keys is None:
            return True
        if force:
            return time.time() - self._forced_at >= self._min_forced_interval
        return time.time() - self._fetched_at > self._ttl

    async def get(self, *, force: bool = False) -> KeySet:
        if not self._stale(force):
            return self._keys  # type: ignore[return-value]
        async with self._lock:
            # Another request may have fetched while this one waited for the lock.
            if not self._stale(force):
                return self._keys  # type: ignore[return-value]
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._uri)
                resp.raise_for_status()
                keys = KeySet.import_key_set(resp.json())
            self._keys = keys
            self._fetched_at = time.time()
            if force:
                self._forced_at = self._fetched_at
            return keys


def _aud_ok(claims: dict, audience: str | None) -> bool:
    if not audience:
        return True
    aud = claims.get("aud")
    if isinstance(aud, str):
        aud = [aud]
    if isinstance(aud, list) and audience in aud:
        return True
    # Many IdPs (e.g. Keycloak) put the client in `azp` rather than `aud`.
    return claims.get("azp") == audience


class OidcAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        jwks_uri: str,
        issuer: str,
        audience: str | None,
        algorithms: list[str],
        username_claim: str,
        allowed_users: set[str],
        jwks_ttl_seconds: int = 3600,
        resource_metadata_url: str | None = None,
        public_paths: set[str] | None = None,
    ) -> None:
        self.app = app
        self._jwks = JwksCache(jwks_uri, jwks_ttl_seconds)
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._algorithms = algorithms or ["RS256"]
        self._username_claim = username_claim
        self._allowed = allowed_users
        self._resource_metadata_url = resource_metadata_url
        self._public_paths = public_paths or set()

        self._claims_registry = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": self._issuer},
            exp={"essential": True},
        )

    def _www_authenticate(self, request: Request) -> str:
        base = 'Bearer realm="wger-mcp"'
        url = self._resource_metadata_url
        if url is None:
            origin = forwarded_origin(request)
            url = origin + WELL_KNOWN_PATH if origin else None
        if url:
            base += f', resource_metadata="{url}"'
        return base

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if is_bypass_path(scope.get("path", ""), self._public_paths):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            await reply_unauthorized(
                scope, receive, send,
                reason="missing bearer token",
                www_authenticate=self._www_authenticate(request),
            )
            return

        token = auth_header.split(" ", 1)[1].strip()
        try:
            claims = await self._verify(token)
        except JoseError as exc:
            log.warning("oidc token rejected: %s", exc)
            await reply_unauthorized(
                scope, receive, send,
                reason=f"invalid token: {exc}",
                www_authenticate=self._www_authenticate(request),
            )
            return

        if not _aud_ok(claims, self._audience):
            await reply_unauthorized(
                scope, receive, send,
                reason="audience mismatch",
                www_authenticate=self._www_authenticate(request),
            )
            return

        username = claims.get(self._username_claim)
        if self._allowed and username not in self._allowed:
            log.warning("user %r not in allowed list", username)
            await reply_unauthorized(
                scope, receive, send,
                reason="user not allowed",
                www_authenticate=self._www_authenticate(request),
            )
            return

        subject = str(claims.get("sub") or username or "unknown")
        identity = Identity(
            subject=subject,
            username=username,
            inbound_token=token,
            strategy="oidc",
            claims=dict(claims),
        )
        ctx = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(ctx)

    async def _verify(self, token: str) -> dict:
        keys = await self._jwks.get()
        try:
            decoded = jwt.decode(token, keys, algorithms=self._algorithms)
        except InvalidKeyIdError:
            # The token names a signing key this set does not hold, which is
            # what a rotation at the IdP looks like from here. Every other
            # JoseError — bad signature, malformed token, unsupported
            # algorithm — describes the token rather than our keys, and
            # refetching on those turned any junk token into a request to the
            # IdP. (A token carrying no kid at all fails as a bad signature, so
            # a rotation is picked up by the TTL rather than here; IdPs that
            # rotate keys publish a kid.)
            keys = await self._jwks.get(force=True)
            decoded = jwt.decode(token, keys, algorithms=self._algorithms)
        self._claims_registry.validate(decoded.claims)
        return decoded.claims
