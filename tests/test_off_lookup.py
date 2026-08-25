"""Tests for the Open Food Facts lookup tools: the shared fetch path.

The single lookup and the batch used to be separate implementations of the
same request, and only the batch retried a 429. They share one path now, so
these check that the single lookup carries what the batch always had, and
still says where a missing product can be added.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from wger_mcp.config import Settings
from wger_mcp.tools import off

OFF_BASE = "https://world.openfoodfacts.org"
BARCODE = "5901234123457"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "wger_base_url": "https://wger.test",
        "mcp_auth": "none",
        "wger_dev_token": "dev",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _register_off(settings: Settings) -> tuple[FastMCP, httpx.AsyncClient]:
    mcp = FastMCP("test")
    http = off.build_http()
    off.register(mcp, http, settings)
    return mcp, http


def _first_result(raw: Any) -> dict[str, Any]:
    """Unwrap FastMCP's call_tool return into the tool's dict payload."""
    if isinstance(raw, dict):
        return raw
    content = raw[0] if isinstance(raw, tuple) else raw
    return json.loads(content[0].text)


def _product() -> dict[str, Any]:
    return {
        "code": BARCODE,
        "product_name": "Dark Chocolate",
        "brands": "SomeBrand",
        "nutriments": {"energy-kcal_100g": 546, "proteins_100g": 7.8},
    }


@pytest.mark.asyncio
async def test_single_lookup_retries_once_on_rate_limit() -> None:
    """The retry lived in the batch path only; the single lookup gets it too."""
    mcp, client = _register_off(_settings())
    async with client:
        with respx.mock(base_url=OFF_BASE) as router:
            route = router.get(f"/api/v2/product/{BARCODE}.json")
            route.side_effect = [
                httpx.Response(429, headers={"retry-after": "0"}),
                httpx.Response(200, json={"status": 1, "product": _product()}),
            ]
            raw = await mcp.call_tool("lookup_food_by_barcode", {"barcode": BARCODE})
    assert route.call_count == 2
    assert _first_result(raw)["name"] == "Dark Chocolate"


@pytest.mark.asyncio
async def test_single_lookup_gives_up_after_the_retry() -> None:
    """A second 429 is reported rather than retried forever."""
    mcp, client = _register_off(_settings())
    async with client:
        with respx.mock(base_url=OFF_BASE) as router:
            route = router.get(f"/api/v2/product/{BARCODE}.json").respond(
                429, headers={"retry-after": "0"}
            )
            raw = await mcp.call_tool("lookup_food_by_barcode", {"barcode": BARCODE})
    assert route.call_count == 2
    result = _first_result(raw)
    assert result["error"] is True
    assert result["status"] == 429


@pytest.mark.asyncio
async def test_missing_product_still_says_where_to_add_it() -> None:
    """The suggestion is the single lookup's own; the batch answers without it."""
    mcp, client = _register_off(_settings())
    async with client:
        with respx.mock(base_url=OFF_BASE) as router:
            router.get(f"/api/v2/product/{BARCODE}.json").respond(
                json={"status": 0, "status_verbose": "product not found"}
            )
            raw = await mcp.call_tool("lookup_food_by_barcode", {"barcode": BARCODE})
            batch = await mcp.call_tool("lookup_foods_by_barcodes", {"barcodes": [BARCODE]})
    single = _first_result(raw)
    assert single["found"] is False
    assert single["detail"] == "product not found"
    assert BARCODE in single["suggestion"]
    assert "suggestion" not in _first_result(batch)["results"][BARCODE]


@pytest.mark.asyncio
async def test_lookup_no_longer_carries_the_wger_payload() -> None:
    """It was shaped for create_ingredient, which wger's read-only /ingredient/
    cannot back. The macros stay, in one shape rather than two."""
    mcp, client = _register_off(_settings())
    async with client:
        with respx.mock(base_url=OFF_BASE) as router:
            router.get(f"/api/v2/product/{BARCODE}.json").respond(
                json={"status": 1, "product": _product()}
            )
            raw = await mcp.call_tool("lookup_food_by_barcode", {"barcode": BARCODE})
    result = _first_result(raw)
    assert "wger_ingredient_payload" not in result
    assert result["macros_per_100g"]["energy_kcal"] == 546
