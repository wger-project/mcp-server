"""Bridge between the generated ``wger_api_client`` and this server's auth.

The generated client expects one fixed token; here the ``Authorization``
header is resolved per request from the caller's identity instead (see
``auth/exchange.py``), so one shared client serves all users. Plus the
offset pagination the tool modules need on top of the generated ``*_list``
endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Protocol

import httpx
from wger_api_client.client import AuthenticatedClient

from . import __version__
from .auth.exchange import WgerTokenProvider
from .config import Settings

# wger caps a page at 999 (WgerLimitOffsetPagination.max_limit)
_PAGE_LIMIT = 999

_USER_AGENT = f"wger-mcp/{__version__}"
# Never sent: _ProviderAuth sets the header on every request. Only the unused
# synchronous client would fall back to it.
_UNUSED_TOKEN = "unused-async-client-authenticates-per-request"


#: How long a single wger call may take. Named because the error path quotes
#: it: an operator reading "no answer within 20s" can tell a slow query from an
#: unreachable server, which "unreachable" alone does not.
REQUEST_TIMEOUT_SECONDS = 20.0


class _ProviderAuth(httpx.Auth):
    """Resolves the Authorization header per request from the token provider."""

    def __init__(self, provider: WgerTokenProvider) -> None:
        self._provider = provider

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = await self._provider.authorization_header()
        yield request


def build_api_client(settings: Settings, provider: WgerTokenProvider) -> AuthenticatedClient:
    """One shared typed client; auth is per-request via ``_ProviderAuth``.

    Only the async half is wired up. The generated client's synchronous
    functions would build their own httpx client from ``token`` and send that
    placeholder as a credential, so this server never calls them.
    """
    base_url = str(settings.wger_base_url).rstrip("/")
    api = AuthenticatedClient(
        base_url=base_url,
        token=_UNUSED_TOKEN,
        raise_on_unexpected_status=True,
    )
    api.set_async_httpx_client(
        httpx.AsyncClient(
            base_url=base_url,
            auth=_ProviderAuth(provider),
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )
    )
    return api


class _Page(Protocol):
    count: Any
    results: Any


async def paginate(
    list_fn: Callable[..., Awaitable[_Page | None]],
    *,
    client: AuthenticatedClient,
    limit: int,
    **filters: Any,
) -> list[dict[str, Any]]:
    """Collect up to ``limit`` items from a generated ``*_list.asyncio``."""
    results: list[dict[str, Any]] = []
    while len(results) < limit:
        asked_for = min(limit - len(results), _PAGE_LIMIT)
        page = await list_fn(
            client=client,
            limit=asked_for,
            offset=len(results) or None,
            **filters,
        )
        items = page.results if page and isinstance(page.results, list) else []
        if not items:
            break
        results.extend(item.to_dict() for item in items)
        # A page shorter than asked for is the last one, with or without a count
        if len(items) < asked_for:
            break
        count = page.count if isinstance(page.count, int) else None
        if count is not None and len(results) >= count:
            break
    return results[:limit]
