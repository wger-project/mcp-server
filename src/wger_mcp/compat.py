"""Warn when the configured wger is older than these tools are written for.

wger publishes its version unauthenticated and its own clients check it — the
flutter app pins a ``MIN_SERVER_VERSION``, and the server-to-server sync command
compares before it starts (``wger/core/api/min_server_version.py``). This is the
same handshake from the other side.

It warns rather than refuses. Most of the surface does not care which release
it talks to — the exercise and ingredient catalogs have been stable for years —
so an operator is better served by a server that starts and one warning than by
one that will not come up.

Deliberately no per-endpoint checks alongside it. Each would encode one
historical difference between two releases, none would help with the next one,
and together they would be a second, worse version of this comparison spread
across the codebase. A tool that comes to need a newer wger raises
:data:`MIN_WGER_VERSION` instead.
"""

from __future__ import annotations

import logging

import httpx
from packaging.version import InvalidVersion, Version
from wger_api_client.api.version import version_retrieve
from wger_api_client.client import Client
from wger_api_client.errors import UnexpectedStatus

from .config import Settings

log = logging.getLogger("wger_mcp")

#: The oldest wger these tools are written for. Raise it with the change that
#: makes a tool need something newer; wger counts its development builds up with
#: every API change, so this can name a point inside a cycle.
#:
#: The spelling decides the meaning, as for the flutter app's MIN_SERVER_VERSION:
#: "2.8.0-dev.3" admits that build and everything after it, "2.8" only the
#: finished release, which until then also shuts out wger.de, as it runs master.
MIN_WGER_VERSION = "2.7"

#: Short: this runs before the server is useful, and a wger that needs longer
#: than this to answer a constant is not one worth waiting on.
_TIMEOUT = 5.0


def _parse(version: object) -> Version | None:
    """A reported version as PEP 440, or None if it is not one."""
    if not isinstance(version, str):
        return None
    try:
        return Version(version)
    except InvalidVersion:
        return None


async def _reported_version(base_url: str) -> str | None:
    """What ``/api/v2/version/`` answers, or None if that cannot be read.

    Deliberately its own unauthenticated client. The endpoint is public and
    *refuses* a credential it cannot verify — a placeholder token answers 403
    where no header at all answers 200 — and the shared client cannot help here
    anyway: it resolves an Authorization header per request from the calling
    user's identity, and at startup there is no caller.
    """
    client = Client(base_url=base_url, raise_on_unexpected_status=True)
    client.set_async_httpx_client(httpx.AsyncClient(base_url=base_url, timeout=_TIMEOUT))
    try:
        return await version_retrieve.asyncio(client=client)
    finally:
        await client.get_async_httpx_client().aclose()


async def check_wger_version(settings: Settings) -> str | None:
    """Compare the configured wger against :data:`MIN_WGER_VERSION`.

    Returns the version it found, or None when that could not be established.
    Nothing here stops the server: an unreachable wger says nothing about
    compatibility, and an old one still serves most of the tools.
    """
    base_url = str(settings.wger_base_url).rstrip("/")
    try:
        found = await _reported_version(base_url)
    except (httpx.HTTPError, UnexpectedStatus, ValueError) as exc:
        # Unreachable, answering with something other than 200, or answering
        # with something that is not JSON — a reverse proxy in front of a wger
        # that is still booting produces all three
        log.warning("could not read the wger version from %s (%s); continuing", base_url, exc)
        return None

    if (reported := _parse(found)) is None:
        log.warning("wger at %s reported an unreadable version %r; continuing", base_url, found)
        return None

    if reported < Version(MIN_WGER_VERSION):
        # Only the two versions and what to do about them. Naming what changed
        # between them would be out of date the moment the floor moves.
        log.warning(
            "the wger at %s is version %s, but this server expects %s or newer. "
            "Most tools will keep working; the ones touching what changed between the two "
            "will not. Please update wger, or use the wger-mcp release matching it.",
            base_url,
            found,
            MIN_WGER_VERSION,
        )
        return found

    log.info("wger at %s is version %s (need >= %s)", base_url, found, MIN_WGER_VERSION)
    return found
