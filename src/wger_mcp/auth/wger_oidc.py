"""Inbound auth for ``MCP_AUTH=wger_oidc``: wger issues the token, we carry it.

Since 2.7 wger is itself an OAuth2/OIDC provider and its REST API accepts the
access tokens it issues, so this server has nothing left to broker: the caller
presents ``Authorization: Bearer <wger-token>`` and that same token goes back
out on the ``/api/v2/`` call (see ``exchange.WgerTokenProvider``).

Those tokens are **opaque** — allauth's default format — so there is nothing to
verify against a JWKS and no claims to read. wger is the only authority on
whether a token is live and which scopes it carries, and it checks that on
every call anyway, so validating here would only add a second, staler source of
truth. The middleware therefore does the little it can do locally (is there a
bearer at all) and leaves the rest to wger.

Two consequences follow, and both are handled here:

- **A bad token is only noticed at the first API call.** Its 401 has to reach
  the client as "re-authenticate" rather than as a tool error; see
  ``tools/common.api_err``.
- **The caller has no name until someone asks wger for it.** Nothing in the
  request path needs one except the optional allowlist, so it is fetched only
  where that makes it necessary, once per token, and cached — keyed by a
  SHA-256 fingerprint, never by the token itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict

import httpx
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .base import is_bypass_path, reply_forbidden, reply_unauthorized
from .identity import Identity, reset_identity, set_identity
from .oauth import WELL_KNOWN_PATH, forwarded_origin

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


def token_fingerprint(token: str) -> str:
    """A stable, non-reversible id for a token.

    Used as the identity's subject and as the username cache key, so that
    neither a log line nor a dictionary in memory ever holds the credential
    itself. Truncated because it identifies rather than authenticates: the full
    digest would be no safer, only longer in logs.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class InvalidTokenError(Exception):
    """wger rejected the token outright (401)."""


class InsufficientScopeError(Exception):
    """The token is live but the grant is missing a scope we need."""

    def __init__(self, scope: str) -> None:
        super().__init__(f'the connection is missing the "{scope}" scope')
        self.scope = scope


class UsernameResolver:
    """Resolves and caches the wger username behind an opaque access token.

    Concurrent requests carrying the same token share one lookup: the cache
    holds the in-flight task, not just the finished answer, so N parallel calls
    from one client cost one round trip rather than N.
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self._url = base_url.rstrip("/") + USERPROFILE_PATH
        self._timeout = timeout
        self._cache: OrderedDict[str, asyncio.Task[str | None]] = OrderedDict()

    async def username_for(self, token: str, fingerprint: str) -> str | None:
        task = self._cache.get(fingerprint)
        if task is not None:
            self._cache.move_to_end(fingerprint)
        else:
            # No await between the lookup and the store, so two requests
            # carrying the same token cannot both start a lookup.
            task = asyncio.create_task(self._fetch(token))
            self._cache[fingerprint] = task
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)

        # Shielded: a client that gives up mid-flight must not cancel the lookup
        # another request is waiting on.
        try:
            username = await asyncio.shield(task)
        except (InvalidTokenError, InsufficientScopeError, httpx.HTTPError):
            self._forget(fingerprint, task)
            raise
        if username is None:
            # wger answered but named nobody. Keeping that would lock the user
            # out for the life of the process over one malformed response.
            self._forget(fingerprint, task)
        return username

    def _forget(self, fingerprint: str, task: asyncio.Task[str | None]) -> None:
        """Drop ``task`` from the cache, unless a newer lookup has replaced it."""
        if self._cache.get(fingerprint) is task:
            del self._cache[fingerprint]

    async def _fetch(self, token: str) -> str | None:
        # A client per lookup rather than a pooled one: this runs once per token,
        # and a long-lived client would need a shutdown hook that ASGI middleware
        # does not get. Same trade-off as JwksCache in ``oidc.py``.
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                self._url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if resp.status_code == 401:
            raise InvalidTokenError
        if resp.status_code == 403:
            raise InsufficientScopeError("api:read")
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            return None
        username = payload.get("username") if isinstance(payload, dict) else None
        return username if isinstance(username, str) and username else None


class WgerBearerMiddleware:
    """Requires a bearer token and binds it as the caller's identity.

    The token is not inspected — see the module docstring. When
    ``MCP_OIDC_ALLOWED_USERS`` is set the username *is* resolved here, before
    the request runs: an allowlist applied after the fact would not be one.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        wger_base_url: str,
        allowed_users: set[str] | None = None,
        resource_metadata_url: str | None = None,
        public_paths: set[str] | None = None,
        resolver: UsernameResolver | None = None,
    ) -> None:
        self.app = app
        self._allowed = allowed_users or set()
        self._resource_metadata_url = resource_metadata_url
        self._public_paths = public_paths or set()
        self._resolver = resolver or UsernameResolver(wger_base_url)

    def _www_authenticate(self, request: Request, *, error: str | None = None) -> str:
        base = 'Bearer realm="wger-mcp"'
        if error:
            base += f', error="{error}"'
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
        if not token:
            await reply_unauthorized(
                scope, receive, send,
                reason="empty bearer token",
                www_authenticate=self._www_authenticate(request),
            )
            return

        fingerprint = token_fingerprint(token)
        username: str | None = None
        if self._allowed:
            try:
                username = await self._resolver.username_for(token, fingerprint)
            except InvalidTokenError:
                log.warning("wger rejected the token of %s", fingerprint)
                await reply_unauthorized(
                    scope, receive, send,
                    reason="wger rejected this token",
                    www_authenticate=self._www_authenticate(
                        request, error="invalid_token"
                    ),
                )
                return
            except InsufficientScopeError as exc:
                await reply_forbidden(
                    scope, receive, send,
                    reason=str(exc),
                    www_authenticate=self._www_authenticate(
                        request, error="insufficient_scope"
                    ),
                )
                return
            except httpx.HTTPError as exc:
                # The allowlist cannot be applied, so the request cannot be let
                # through — but this is our outage, not the caller's fault.
                log.warning("could not reach wger to resolve the caller: %s", exc)
                await reply_unauthorized(
                    scope, receive, send,
                    reason="could not verify the caller against wger",
                    www_authenticate=self._www_authenticate(request),
                )
                return

            if username not in self._allowed:
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
            )
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(ctx)
