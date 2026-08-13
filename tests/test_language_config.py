"""Tests for DEFAULT_LANGUAGE: config validation, OFF field selection, tool defaults."""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from wger_mcp.config import Settings
from wger_mcp.tools import exercises, off
from wger_mcp.wger_client import WgerClient

OFF_BASE = "https://world.openfoodfacts.org"
WGER_API = "https://wger.test/api/v2"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "wger_base_url": "https://wger.test",
        "mcp_auth": "none",
        "wger_dev_token": "dev",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _product(**extra: Any) -> dict[str, Any]:
    prod: dict[str, Any] = {
        "code": "5901234123457",
        "product_name": "Dark Chocolate",
        "brands": "SomeBrand",
        "nutriments": {"energy-kcal_100g": 546, "proteins_100g": 7.8},
    }
    prod.update(extra)
    return prod


def _first_result(raw: Any) -> dict[str, Any]:
    """Unwrap FastMCP's call_tool return into the tool's dict payload."""
    if isinstance(raw, dict):
        return raw
    content = raw[0] if isinstance(raw, tuple) else raw
    block = content[0]
    return json.loads(block.text)


# ---------- config ----------


def test_default_language_defaults_to_en() -> None:
    assert _settings().default_language == "en"


def test_default_language_is_normalized() -> None:
    assert _settings(default_language="  PL ").default_language == "pl"


@pytest.mark.parametrize("bad", ["polish", "p", "pl-PL", ""])
def test_invalid_default_language_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        _settings(default_language=bad)


# ---------- OFF field selection ----------


def test_fields_for_requests_localised_variants() -> None:
    fields = off._fields_for("de").split(",")
    assert "product_name_de" in fields
    assert "ingredients_text_de" in fields
    # No language is baked in beyond the requested one.
    assert not any(f.endswith("_pl") for f in fields)


def test_shape_prefers_localised_name_and_falls_back() -> None:
    prod = _product(product_name_de="Zartbitterschokolade", ingredients_text_de="Kakao")
    shaped = off._shape(prod, "de")
    assert shaped["language"] == "de"
    assert shaped["name"] == "Zartbitterschokolade"
    assert shaped["name_localized"] == "Zartbitterschokolade"
    assert shaped["name_default"] == "Dark Chocolate"
    assert shaped["ingredients_text"] == "Kakao"

    # Missing localised name -> language-neutral product_name.
    fallback = off._shape(_product(), "de")
    assert fallback["name"] == "Dark Chocolate"
    assert fallback["name_localized"] is None
    assert fallback["ingredients_text"] is None


def test_shape_treats_empty_localised_fields_as_absent() -> None:
    """OFF commonly returns '' rather than omitting a per-language field.

    Verified against live OFF data (e.g. barcode 3017620422003 has
    ``product_name_pt == ''``), so the empty string must fall back exactly
    like a missing key does.
    """
    prod = _product(product_name_pt="", ingredients_text_pt="")
    shaped = off._shape(prod, "pt")
    assert shaped["name"] == "Dark Chocolate"
    assert shaped["name_localized"] is None
    assert shaped["ingredients_text"] is None


def test_shape_normalizes_list_valued_fields_consistently() -> None:
    """OFF sometimes returns a list where a string is expected.

    ``name`` has always collapsed such a list to its first entry; the localised
    and default name fields must not disagree with it.
    """
    prod = _product(
        product_name=["Dark Chocolate", "Alt"],
        product_name_de=["Zartbitter", "Alt"],
        brands=["SomeBrand", "Other"],
    )
    shaped = off._shape(prod, "de")
    assert shaped["name"] == "Zartbitter"
    assert shaped["name_localized"] == "Zartbitter"
    assert shaped["name_default"] == "Dark Chocolate"
    assert shaped["brand"] == "SomeBrand"


# ---------- tool wiring ----------


def _register_off(settings: Settings) -> tuple[FastMCP, WgerClient]:
    mcp = FastMCP("test")
    client = WgerClient("https://wger.test/api/v2", _StubProvider())
    off.register(mcp, client, settings)
    return mcp, client


@pytest.mark.asyncio
async def test_lookup_uses_configured_default_language() -> None:
    mcp, client = _register_off(_settings(default_language="fr"))
    async with client:
        with respx.mock(base_url=OFF_BASE) as router:
            route = router.get("/api/v2/product/5901234123457.json").respond(
                json={"status": 1, "product": _product(product_name_fr="Chocolat noir")}
            )
            raw = await mcp.call_tool(
                "lookup_food_by_barcode", {"barcode": "5901234123457"}
            )
    assert "product_name_fr" in route.calls.last.request.url.params["fields"]
    result = _first_result(raw)
    assert result["language"] == "fr"
    assert result["name"] == "Chocolat noir"


@pytest.mark.asyncio
async def test_per_call_language_overrides_default() -> None:
    mcp, client = _register_off(_settings(default_language="en"))
    async with client:
        with respx.mock(base_url=OFF_BASE) as router:
            route = router.get("/api/v2/product/5901234123457.json").respond(
                json={"status": 1, "product": _product(product_name_pl="Gorzka czekolada")}
            )
            raw = await mcp.call_tool(
                "lookup_food_by_barcode",
                {"barcode": "5901234123457", "language": "pl"},
            )
    assert "product_name_pl" in route.calls.last.request.url.params["fields"]
    result = _first_result(raw)
    assert result["language"] == "pl"
    assert result["name"] == "Gorzka czekolada"


@pytest.mark.asyncio
async def test_batch_lookup_threads_language() -> None:
    mcp, client = _register_off(_settings(default_language="es"))
    async with client:
        with respx.mock(base_url=OFF_BASE) as router:
            route = router.get("/api/v2/product/5901234123457.json").respond(
                json={"status": 1, "product": _product(product_name_es="Chocolate negro")}
            )
            raw = await mcp.call_tool(
                "lookup_foods_by_barcodes", {"barcodes": ["5901234123457"]}
            )
    assert "product_name_es" in route.calls.last.request.url.params["fields"]
    result = _first_result(raw)
    assert result["results"]["5901234123457"]["name"] == "Chocolate negro"


def _register_exercises(settings: Settings) -> tuple[FastMCP, WgerClient]:
    mcp = FastMCP("test")
    client = WgerClient(WGER_API, _StubProvider())
    exercises.register(mcp, client, settings)
    return mcp, client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "passed", "expected"),
    [("en", None, "en"), ("de", None, "de"), ("de", "pl", "pl")],
)
async def test_exercise_search_language_resolution(
    configured: str, passed: str | None, expected: str
) -> None:
    mcp, client = _register_exercises(_settings(default_language=configured))
    args: dict[str, Any] = {"query": "squat"}
    if passed is not None:
        args["language"] = passed
    async with client:
        with respx.mock(base_url=WGER_API) as router:
            # search now resolves the language code to wger's numeric id so it can
            # pick the translation in the requested language
            router.get("/language/").respond(
                json={"count": 1, "next": None, "results": [{"id": 2, "short_name": "en"}]}
            )
            route = router.get("/exerciseinfo/").respond(json={"results": [], "next": None})
            await mcp.call_tool("search_exercises", args)
    assert route.calls.last.request.url.params["language__code"] == expected
