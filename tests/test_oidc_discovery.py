"""Endpoint discovery, and what happens when the provider is not up yet."""

from __future__ import annotations

import httpx
import pytest
import respx

from wger_mcp import server
from wger_mcp.auth.oidc_discovery import OidcDiscoveryError, discover_endpoints

from .conftest import WGER_BASE, WGER_DISCOVERY, wger_discovery_doc


def test_a_provider_that_is_still_booting_is_waited_for() -> None:
    """wger started next to this server in one compose file answers a moment
    later; crashing on the first refusal made that a restart loop."""
    with respx.mock() as router:
        route = router.get(WGER_DISCOVERY)
        route.mock(
            side_effect=[
                httpx.ConnectError("refused"),
                httpx.Response(502),
                httpx.Response(200, json=wger_discovery_doc()),
            ]
        )
        eps = discover_endpoints(WGER_BASE, retry_delays=(0, 0))
    assert eps.token_endpoint.startswith(WGER_BASE)
    assert route.call_count == 3


def test_it_gives_up_after_the_last_retry() -> None:
    with respx.mock() as router:
        route = router.get(WGER_DISCOVERY).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(OidcDiscoveryError, match="refused"):
            discover_endpoints(WGER_BASE, retry_delays=(0, 0))
    assert route.call_count == 3


def test_a_client_error_is_not_retried() -> None:
    """A 404 is a wrong URL, not a provider that is still starting."""
    with respx.mock() as router:
        route = router.get(WGER_DISCOVERY).respond(404)
        with pytest.raises(OidcDiscoveryError):
            discover_endpoints(WGER_BASE, retry_delays=(0, 0))
    assert route.call_count == 1


def test_the_token_endpoint_auth_methods_are_read() -> None:
    doc = {**wger_discovery_doc(), "token_endpoint_auth_methods_supported": ["none"]}
    with respx.mock() as router:
        router.get(WGER_DISCOVERY).respond(json=doc)
        eps = discover_endpoints(WGER_BASE, retry_delays=())
    assert eps.token_endpoint_auth_methods == ["none"]


def test_main_reports_a_failed_discovery_as_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The operator's answer is "wger is not reachable", not a traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_AUTH", "wger_oidc")

    def fail(settings: object) -> None:
        raise OidcDiscoveryError("OIDC discovery failed for https://wger.test: refused")

    monkeypatch.setattr(server, "build_app", fail)
    with pytest.raises(SystemExit) as exc:
        server.main(["--transport", "http"])
    assert "refused" in str(exc.value)
