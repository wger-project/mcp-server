"""Refuse to run against a wger too old for the API these tools speak.

wger publishes its version unauthenticated and its own clients check it — the
flutter app pins a ``MIN_SERVER_VERSION``, and the server-to-server sync command
compares before it starts (``wger/core/api/min_server_version.py``). This is the
same handshake from the other side.

It warns rather than refuses, and it is the *only* compatibility check here.
Most of the surface does not care which release it talks to — the exercise and
ingredient catalogs have been stable for years — so an operator is better served
by a server that starts and one warning than by one that will not come up.

Deliberately no per-endpoint checks alongside it. Each would encode one
historical difference between two releases, none would help with the next one,
and together they would be a second, worse version of this comparison spread
across the codebase. One version, one warning, and the advice to keep the client
current.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version

import httpx
from packaging.version import InvalidVersion, Version
from wger_api_client.api.version import version_retrieve
from wger_api_client.client import Client
from wger_api_client.errors import UnexpectedStatus

from .config import Settings

log = logging.getLogger("wger_mcp")

#: The generated client, whose version says which wger release it targets.
API_CLIENT = "wger-api-client"

#: Short: this runs before the server is useful, and a wger that needs longer
#: than this to answer a constant is not one worth waiting on.
_TIMEOUT = 5.0


def required_version() -> tuple[int, ...] | None:
    """The wger release the installed API client is built for, or None.

    Read off the client rather than written down here, so the two cannot drift:
    its README makes the rule explicit — "the major and minor version indicate
    which wger release this client targets, so 2.6.x is meant for a 2.6 server",
    with the patch reserved for changes to the package itself. Hence major and
    minor only.

    A consequence worth knowing: upgrading the client raises this floor. That is
    the intent — the two are meant to move together — but it means a client
    upgrade can refuse a wger that worked the day before, loudly rather than by
    misbehaving.
    """
    try:
        return Version(installed_version(API_CLIENT)).release[:2]
    except (PackageNotFoundError, InvalidVersion):
        return None


def _release(version: str) -> tuple[int, ...] | None:
    """The numeric part of a version, or None if it is not one.

    Only the release components are compared, so a pre-release of a supported
    version counts as that version: 2.7.0a2 has the 2.7 API, and locking out the
    people testing a release candidate is precisely the wrong audience to lock
    out. PEP 440 would order it below 2.7 instead.
    """
    try:
        return Version(version).release
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
    """Compare the configured wger against what the API client targets.

    Returns the version it found, or None when that could not be established.
    Nothing here stops the server: an unreachable wger says nothing about
    compatibility, and an old one still serves most of the tools.
    """
    needed = required_version()
    if needed is None:
        log.warning("cannot read the version of %s; skipping the wger check", API_CLIENT)
        return None

    base_url = str(settings.wger_base_url).rstrip("/")
    try:
        found = await _reported_version(base_url)
    except (httpx.HTTPError, UnexpectedStatus, ValueError) as exc:
        # Unreachable, answering with something other than 200, or answering
        # with something that is not JSON — a reverse proxy in front of a wger
        # that is still booting produces all three
        log.warning("could not read the wger version from %s (%s); continuing", base_url, exc)
        return None

    if not isinstance(found, str) or (release := _release(found)) is None:
        log.warning("wger at %s reported an unreadable version %r; continuing", base_url, found)
        return None

    minimum = ".".join(str(part) for part in needed)
    if release < needed:
        # Only the two versions and what to do about them. Naming what changed
        # between them would be out of date the moment the floor moves, which it
        # now does on its own whenever the client is upgraded.
        log.warning(
            "the wger at %s is version %s, but this server expects %s or newer (%s %s). "
            "Most tools will keep working; the ones touching what changed between the two "
            "will not. Please update wger, or install the %s release matching it.",
            base_url,
            found,
            minimum,
            API_CLIENT,
            installed_version(API_CLIENT),
            API_CLIENT,
        )
        return found

    log.info("wger at %s is version %s (need >= %s)", base_url, found, minimum)
    return found
