"""The version handshake with wger.

wger's own clients check this — the flutter app pins a MIN_SERVER_VERSION and
the sync command compares before it starts. What makes it worth doing here is
that the failure it replaces is unreadable: against 2.6 the tools raise a
TypeError deep in the generated client, refuse valid ids as malformed, and drop
a date filter silently.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from wger_mcp.compat import (
    check_wger_version,
    required_version,
)
from wger_mcp.config import Settings


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )


def _answering(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Serve /api/v2/version/ from `handler` instead of the network."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


@pytest.fixture(autouse=True)
def _floor_at_2_7(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the derived floor, so these tests describe the comparison rather than
    whichever client happens to be installed."""
    monkeypatch.setattr("wger_mcp.compat.required_version", lambda: (2, 7))


def _returning(version, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/version/"
        return httpx.Response(status, json=version)

    return handler


@pytest.mark.asyncio
async def test_a_supported_version_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _answering(monkeypatch, _returning("2.7.0"))
    assert await check_wger_version(_settings()) == "2.7.0"


@pytest.mark.asyncio
async def test_a_newer_version_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _answering(monkeypatch, _returning("2.9.1"))
    assert await check_wger_version(_settings()) == "2.9.1"


@pytest.mark.asyncio
async def test_a_prerelease_of_a_supported_version_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PEP 440 orders 2.7.0a2 below 2.7, but it has the 2.7 API — and the people
    running a release candidate are the worst possible audience to lock out."""
    _answering(monkeypatch, _returning("2.7.0a2"))
    assert await check_wger_version(_settings()) == "2.7.0a2"


@pytest.mark.asyncio
async def test_an_older_version_warns_but_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Most of the surface does not care which release it talks to — the
    catalogs have been stable for years — so refusing to start would take away
    the tools that work along with the ones that do not."""
    _answering(monkeypatch, _returning("2.6.1"))
    with caplog.at_level("WARNING", logger="wger_mcp"):
        assert await check_wger_version(_settings()) == "2.6.1"
    message = caplog.text
    assert "2.6.1" in message  # what it found
    assert "2.7" in message  # what it expects


@pytest.mark.asyncio
async def test_an_unreachable_wger_does_not_block_the_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether wger happens to be up when this process starts says nothing about
    compatibility. Coupling our boot to theirs turns a restart order into an
    outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _answering(monkeypatch, handler)
    assert await check_wger_version(_settings()) is None


@pytest.mark.asyncio
async def test_a_server_error_does_not_block_the_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answering(monkeypatch, _returning("2.7.0", status=503))
    assert await check_wger_version(_settings()) is None


@pytest.mark.asyncio
async def test_an_unreadable_version_does_not_block_the_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reverse proxy answering with something else, or a fork with its own
    version scheme. Refusing on that would be guessing."""
    _answering(monkeypatch, _returning("dev"))
    assert await check_wger_version(_settings()) is None


@pytest.mark.asyncio
async def test_a_non_string_version_does_not_block_the_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answering(monkeypatch, _returning({"version": "2.7.0"}))
    assert await check_wger_version(_settings()) is None


# ---------- the boot is actually wired to it ----------


@pytest.mark.asyncio
async def test_the_http_boot_runs_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check is only worth having if it runs. build_app's lifespan is the
    documented ASGI entry point, so that is where an operator's uvicorn meets
    it — hence the assertion here rather than trusting the call site."""
    import os

    from wger_mcp.config import load_settings
    from wger_mcp.server import build_app

    seen: list[object] = []

    async def _record(settings: object) -> None:
        seen.append(settings)

    monkeypatch.setattr("wger_mcp.server.check_wger_version", _record)
    os.environ["MCP_AUTH"] = "none"
    os.environ["WGER_DEV_TOKEN"] = "dev"
    app = build_app(load_settings(env_file=None))

    with TestClient(app):
        pass
    assert seen, "the lifespan did not run the version check"


# ---------- where the floor comes from ----------


def test_the_floor_is_the_release_the_client_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read off wger-api-client rather than written down, so the two cannot
    drift. Its README states the rule: 2.6.x targets a 2.6 server, the patch
    component belongs to the package itself.

    The autouse fixture pins the module attribute, which is what
    check_wger_version looks up; the name imported here is still the real
    function, so this exercises the derivation itself.
    """
    monkeypatch.setattr("wger_mcp.compat.installed_version", lambda name: "2.7.3")
    assert required_version() == (2, 7)


def test_an_uninstallable_client_version_skips_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import PackageNotFoundError

    def _missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("wger_mcp.compat.installed_version", _missing)
    assert required_version() is None


@pytest.mark.asyncio
async def test_without_a_known_floor_nothing_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wger_mcp.compat.required_version", lambda: None)
    _answering(monkeypatch, _returning("2.0.0"))
    assert await check_wger_version(_settings()) is None
