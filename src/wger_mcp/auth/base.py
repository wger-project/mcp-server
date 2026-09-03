"""Common helpers for auth middlewares."""

from __future__ import annotations

import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .identity import Identity, reset_identity, set_identity

log = logging.getLogger(__name__)

_BYPASS_EXACT = {"/health"}


def is_bypass_path(path: str, extra: set[str] | None = None) -> bool:
    """Public paths that skip inbound auth: health, OAuth discovery metadata, and
    the AS-facade endpoints (``extra``; they carry their own OAuth client auth)."""
    return (
        path in _BYPASS_EXACT
        or (extra is not None and path in extra)
        or path.startswith("/health/")
        or path.startswith("/.well-known/")
    )


async def reply_unauthorized(
    scope: Scope, receive: Receive, send: Send, *, reason: str, www_authenticate: str
) -> None:
    resp = JSONResponse(
        {"error": "unauthorized", "reason": reason},
        status_code=401,
        headers={"www-authenticate": www_authenticate},
    )
    await resp(scope, receive, send)


async def reply_forbidden(
    scope: Scope, receive: Receive, send: Send, *, reason: str, www_authenticate: str
) -> None:
    """403, for a token that is valid but does not carry the required scope.

    Distinct from 401 on purpose: a client that sees 401 re-runs the OAuth flow
    and gets the same token back, because the grant — not the token — is what
    is short. RFC 6750 says ``insufficient_scope`` with the scope named, which
    is what tells the user their connection has to be re-authorized.
    """
    resp = JSONResponse(
        {"error": "insufficient_scope", "reason": reason},
        status_code=403,
        headers={"www-authenticate": www_authenticate},
    )
    await resp(scope, receive, send)


class NoAuthMiddleware:
    """No-op middleware. Use only for local dev (``MCP_AUTH=none``).

    Binds a fixed dev :class:`Identity`; the wger client then uses the static
    ``WGER_API_KEY`` for outbound calls.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        log.warning("MCP_AUTH=none — incoming requests are NOT authenticated")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = set_identity(Identity(subject="local-dev", username="local-dev", strategy="none"))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(token)


class StaticTokenMiddleware:
    """Shared-secret bearer auth (``MCP_AUTH=static_token``).

    Single-user: every authenticated caller acts as the one wger account behind
    ``WGER_API_KEY``. Unlike ``none`` this *does* gate inbound requests, so it
    is safe to expose over TLS — but the secret grants full access to that
    account, so treat it like a password.
    """

    def __init__(self, app: ASGIApp, *, token: str, username: str = "static-token") -> None:
        self.app = app
        self._token = token
        self._username = username

    @staticmethod
    def _www_authenticate() -> str:
        return 'Bearer realm="wger-mcp"'

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if is_bypass_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            await reply_unauthorized(
                scope, receive, send,
                reason="missing bearer token",
                www_authenticate=self._www_authenticate(),
            )
            return

        presented = auth_header.split(" ", 1)[1].strip()
        # Constant-time compare so a wrong token leaks no timing signal.
        if not hmac.compare_digest(presented, self._token):
            log.warning("static token rejected")
            await reply_unauthorized(
                scope, receive, send,
                reason="invalid token",
                www_authenticate=self._www_authenticate(),
            )
            return

        ctx = set_identity(
            Identity(
                subject=self._username,
                username=self._username,
                strategy="static_token",
            )
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(ctx)
