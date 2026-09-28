"""The per-request caller identity, carried via a context variable.

The inbound auth middleware resolves the caller (from an SSO/OIDC token, or a
fixed dev identity for ``MCP_AUTH=none``) and binds an :class:`Identity` for the
duration of the request. The outbound auth reads it to obtain a wger
credential for that specific user. Using a ``ContextVar`` keeps the FastMCP
tools — which only receive the shared API client — unaware of the request
plumbing.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Identity:
    """Who is making the current request.

    ``subject`` is the stable cache key (IdP ``sub``). ``username`` is the
    human-facing name. ``inbound_token`` is the raw SSO token to be exchanged
    for a wger credential; it is ``None`` in dev mode. ``requested_scopes`` is
    what this server asks the provider for (``wger_oidc`` only).
    """

    subject: str
    username: str | None = None
    inbound_token: str | None = None
    strategy: str = "oidc"
    claims: dict[str, Any] = field(default_factory=dict)
    requested_scopes: frozenset[str] = frozenset()


_current: ContextVar[Identity | None] = ContextVar("wger_mcp_identity", default=None)


def set_identity(identity: Identity):
    """Bind ``identity`` to the current context. Returns the reset token."""
    return _current.set(identity)


def reset_identity(token) -> None:
    _current.reset(token)


def current_identity() -> Identity | None:
    return _current.get()
