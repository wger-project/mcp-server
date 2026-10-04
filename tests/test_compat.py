"""The version handshake with wger.

wger's own clients check this — the flutter app pins a MIN_SERVER_VERSION and
the sync command compares before it starts. What makes it worth doing here is
that the failure it replaces is unreadable: against 2.6 the tools raise a
TypeError deep in the generated client, refuse valid ids as malformed, and drop
a date filter silently.
"""

from __future__ import annotations

from importlib.metadata import version as installed_version

import httpx
import pytest
from packaging.version import Version
from starlette.testclient import TestClient

from wger_mcp.compat import MIN_WGER_VERSION, check_wger_version
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
def _floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the floor, so these tests describe the comparison rather than
    whatever the tools need today."""
    monkeypatch.setattr("wger_mcp.compat.MIN_WGER_VERSION", "2.8.0-dev.3")


def _returning(version, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/version/"
        return httpx.Response(status, json=version)

    return handler


async def _warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reported: str
) -> bool:
    """Whether a wger reporting `reported` is warned about. It starts either way."""
    _answering(monkeypatch, _returning(reported))
    with caplog.at_level("WARNING", logger="wger_mcp"):
        assert await check_wger_version(_settings()) == reported
    return any(record.name == "wger_mcp" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported",
    ["2.8.0-dev.3", "2.8.0-dev.10", "2.8.0-rc.1", "2.8.0", "2.9.0-dev.0", "2.9.1"],
)
async def test_the_floor_and_what_follows_it_pass(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reported: str
) -> None:
    """dev.10 follows dev.3: the build number is counted, not compared as text."""
    assert not await _warns(monkeypatch, caplog, reported)


@pytest.mark.asyncio
async def test_an_older_version_warns_but_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Most of the surface does not care which release it talks to — the
    catalogs have been stable for years — so refusing to start would take away
    the tools that work along with the ones that do not."""
    assert await _warns(monkeypatch, caplog, "2.7.0")
    assert "2.7.0" in caplog.text  # what it found
    assert "2.8.0-dev.3" in caplog.text  # what it expects


@pytest.mark.asyncio
async def test_an_earlier_build_of_the_cycle_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A self-hosted master from before the change the floor names."""
    assert await _warns(monkeypatch, caplog, "2.8.0-dev.2")


@pytest.mark.asyncio
async def test_a_release_floor_warns_about_its_development_builds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The spelling decides: "2.8" is the finished release, which a master
    reporting 2.8.0-dev.N has not reached yet, wger.de among them."""
    monkeypatch.setattr("wger_mcp.compat.MIN_WGER_VERSION", "2.8")
    assert await _warns(monkeypatch, caplog, "2.8.0-dev.5")


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


# ---------- the floor itself ----------


def test_the_floor_keeps_up_with_the_client() -> None:
    """The client targets one wger release (by its README, 2.7.x is meant for a
    2.7 server), so a floor from an older one would let through a wger the
    client cannot talk to. Raising the client without the floor fails here.

    MIN_WGER_VERSION is the name imported above, which the autouse fixture does
    not reach.
    """
    client = Version(installed_version("wger-api-client")).release[:2]
    assert Version(MIN_WGER_VERSION).release[:2] >= client
