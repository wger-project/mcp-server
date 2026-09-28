"""Inbound auth for ``MCP_AUTH=wger_oidc``: wger issues the token, we carry it.

Since 2.7 wger is itself an OAuth2/OIDC provider and its REST API accepts the
access tokens it issues, so this server has nothing left to broker: the caller
presents ``Authorization: Bearer <wger-token>`` and that same token goes back
out on the ``/api/v2/`` call (see ``exchange.WgerTokenProvider``).

Those tokens are **opaque** — allauth's default format — so the only way to
check one is to ask wger. The middleware does that with ``/api/v2/userprofile/``
and caches the answer for :data:`_TTL_SECONDS`, keyed by a SHA-256 fingerprint:
a dead token has to be answered with an HTTP 401, because that is what makes a
client refresh it. A tool result carrying wger's refusal arrives inside a 200.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import httpx
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .base import (
    bearer_token,
    is_bypass_path,
    reply_forbidden,
    reply_unauthorized,
    reply_unavailable,
    www_authenticate,
)
from .identity import Identity, reset_identity, set_identity

log = logging.getLogger(__name__)

#: Where to ask wger who the bearer of a token is. The OIDC ``userinfo``
#: endpoint would be the textbook answer, but it only returns a username when
#: the grant carries the ``profile`` scope, which this server does not request.
#: The profile endpoint needs ``api:read`` — which every grant here has, since
#: without it no tool would work either.
USERPROFILE_PATH = "/api/v2/userprofile/"

#: Distinct usernames kept in memory. One entry per live token, so the ceiling
#: is really "how many clients are connected at once"; the eviction order is
#: LRU so a busy caller is never the one dropped.
_CACHE_MAX = 1024

#: How long a positive answer is trusted. Bounds how late an expired or revoked
#: token is noticed; tool calls in that window still get wger's refusal.
_TTL_SECONDS = 60.0

#: How long a refusal is remembered, so a token sent again and again costs wger
#: one lookup. Short: a client that got the 401 comes back with a new token.
_NEGATIVE_TTL_SECONDS = 10.0

#: Lookups running against wger at once. Anyone can send a bearer, and a lookup
#: outlives a client that hangs up, so without a cap the lookups in flight
#: would not be bounded by the connections open. Past it the answer is a 503.
_MAX_IN_FLIGHT = 32


def token_fingerprint(token: str) -> str:
    """A stable, non-reversible id for a token.

    Used as the identity's subject and as the username cache key, so that
    neither a log line nor a dictionary in memory ever holds the credential
    itself. Truncated because it identifies rather than authenticates: the full
    digest would be no safer, only longer in logs.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


#: wger's 403 for a grant without the scope: 'The access token is missing the
#: "api:write" scope.'
_MISSING_SCOPE = re.compile(r'missing the ["\'](api:[a-z]+)["\'] scope')


def missing_scope(detail: Any) -> str | None:
    """The scope a wger error body names as missing, if that is its complaint."""
    match = _MISSING_SCOPE.search(str(detail))
    return match.group(1) if match else None


def token_rejected(status: int, detail: Any) -> bool:
    """Whether wger refused the token itself (expired, revoked, unknown).

    wger says so with a 403, not a 401: SessionAuthentication heads its DRF
    authentication classes, and DRF then turns every 401 into a 403. What
    marks it is simplejwt's ``token_not_valid``, the last class in the chain.
    """
    if status == 401:
        return True
    return status == 403 and isinstance(detail, dict) and detail.get("code") == "token_not_valid"


def _error_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


class InvalidTokenError(Exception):
    """wger does not accept the token."""


class InsufficientScopeError(Exception):
    """The token is live but the grant is missing a scope we need."""

    def __init__(self, scope: str) -> None:
        super().__init__(f'the connection is missing the "{scope}" scope')
        self.scope = scope


class LookupOverloadedError(Exception):
    """Too many lookups are already running against wger."""


class UsernameResolver:
    """Checks an opaque access token against wger and resolves its username.

    Concurrent requests carrying the same token share one lookup: the cache
    holds the in-flight task, not just the finished answer, so N parallel calls
    from one client cost one round trip rather than N.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        ttl_seconds: float = _TTL_SECONDS,
        negative_ttl_seconds: float = _NEGATIVE_TTL_SECONDS,
        max_in_flight: int = _MAX_IN_FLIGHT,
    ) -> None:
        self._url = base_url.rstrip("/") + USERPROFILE_PATH
        self._timeout = timeout
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._max_in_flight = max_in_flight
        self._in_flight = 0
        self._cache: OrderedDict[str, tuple[float, asyncio.Task[str | None]]] = OrderedDict()
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    async def username_for(self, token: str, fingerprint: str) -> str | None:
        """The token's username; raises if wger refuses the token."""
        entry = self._cache.get(fingerprint)
        if entry is not None and self._fresh(*entry):
            task = entry[1]
            self._cache.move_to_end(fingerprint)
        else:
            if self._in_flight >= self._max_in_flight:
                raise LookupOverloadedError
            # No await between the lookup and the store, so two requests
            # carrying the same token cannot both start a lookup.
            self._in_flight += 1
            task = asyncio.create_task(self._fetch(token))
            task.add_done_callback(self._lookup_done)
            self._cache[fingerprint] = (time.monotonic(), task)
            self._cache.move_to_end(fingerprint)
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)

        # Shielded: a client that gives up mid-flight must not cancel the lookup
        # another request is waiting on.
        try:
            username = await asyncio.shield(task)
        except httpx.HTTPError:
            # Our outage, not the token's: the next request asks again.
            self._forget(fingerprint, task)
            raise
        if username is None:
            # wger answered but named nobody. Keeping that would lock the user
            # out of an allowlist over one malformed response.
            self._forget(fingerprint, task)
        return username

    def _fresh(self, created: float, task: asyncio.Task[str | None]) -> bool:
        if not task.done():
            return True
        if task.cancelled():
            return False
        age = time.monotonic() - created
        exc = task.exception()
        if exc is None:
            return age < self._ttl
        if isinstance(exc, (InvalidTokenError, InsufficientScopeError)):
            return age < self._negative_ttl
        return False

    def _lookup_done(self, _task: asyncio.Task[str | None]) -> None:
        self._in_flight -= 1

    def _forget(self, fingerprint: str, task: asyncio.Task[str | None]) -> None:
        """Drop ``task`` from the cache, unless a newer lookup has replaced it."""
        entry = self._cache.get(fingerprint)
        if entry is not None and entry[1] is task:
            del self._cache[fingerprint]

    def _http(self) -> httpx.AsyncClient:
        # One pooled client per event loop: a connection belongs to the loop
        # that opened it, and tests drive the app from more than one.
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=self._max_in_flight),
            )
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _fetch(self, token: str) -> str | None:
        resp = await self._http().get(
            self._url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if resp.status_code in (401, 403):
            # The user's own profile has no permission check beyond the token,
            # so a 403 that names no scope means the token is no good here —
            # expired, revoked, or bound to another resource.
            if scope := missing_scope(_error_body(resp)):
                raise InsufficientScopeError(scope)
            raise InvalidTokenError
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            return None
        username = payload.get("username") if isinstance(payload, dict) else None
        return username if isinstance(username, str) and username else None


class WgerBearerMiddleware:
    """Requires a bearer token wger accepts and binds it as the caller's identity."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        wger_base_url: str,
        allowed_users: set[str] | None = None,
        resource_metadata_url: str | None = None,
        public_paths: set[str] | None = None,
        scopes: list[str] | None = None,
        resolver: UsernameResolver | None = None,
    ) -> None:
        self.app = app
        self._allowed = allowed_users or set()
        # Named in the insufficient_scope challenge: what a client re-authorizing
        # has to ask for. The whole set, since a grant of the missing one alone
        # would lose the rest.
        self._scopes = " ".join(scopes) if scopes else None
        self._requested = frozenset(scopes or ())
        self._resource_metadata_url = resource_metadata_url
        self._public_paths = public_paths or set()
        self._resolver = resolver or UsernameResolver(wger_base_url)

    def _closing_on_shutdown(self, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "lifespan.shutdown.complete":
                await self._resolver.aclose()
            await send(message)

        return wrapped

    def _www_authenticate(
        self, request: Request, *, error: str | None = None, scope: str | None = None
    ) -> str:
        return www_authenticate(request, self._resource_metadata_url, error=error, scope=scope)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, self._closing_on_shutdown(send))
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if is_bypass_path(scope.get("path", ""), self._public_paths):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        token = bearer_token(request)
        if token is None:
            await reply_unauthorized(
                scope, receive, send,
                reason="missing bearer token",
                www_authenticate=self._www_authenticate(request),
            )
            return

        if not token:
            await reply_unauthorized(
                scope, receive, send,
                reason="empty bearer token",
                www_authenticate=self._www_authenticate(request),
            )
            return

        fingerprint = token_fingerprint(token)
        try:
            username = await self._resolver.username_for(token, fingerprint)
        except InvalidTokenError:
            log.info("wger rejected the token of %s", fingerprint)
            await reply_unauthorized(
                scope, receive, send,
                reason="wger rejected this token",
                www_authenticate=self._www_authenticate(request, error="invalid_token"),
            )
            return
        except InsufficientScopeError as exc:
            await reply_forbidden(
                scope, receive, send,
                reason=str(exc),
                www_authenticate=self._www_authenticate(
                    request, error="insufficient_scope", scope=self._scopes or exc.scope
                ),
            )
            return
        except LookupOverloadedError:
            log.warning("too many token checks in flight; answering 503")
            await reply_unavailable(
                scope, receive, send, reason="too many token checks in flight"
            )
            return
        except httpx.HTTPError as exc:
            # Not a 401: that would make the client throw away a token that
            # may well be fine.
            log.warning("could not reach wger to check the caller's token: %s", exc)
            await reply_unavailable(
                scope, receive, send, reason="could not check the token with wger"
            )
            return

        if self._allowed and username not in self._allowed:
            log.warning("user %r not in allowed list", username)
            await reply_unauthorized(
                scope, receive, send,
                reason="user not allowed",
                www_authenticate=self._www_authenticate(request),
            )
            return

        ctx = set_identity(
            Identity(
                subject=fingerprint,
                username=username,
                inbound_token=token,
                strategy="wger_oidc",
                requested_scopes=self._requested,
            )
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(ctx)
