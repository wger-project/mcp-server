"""Open Food Facts lookup tools.

OFF is a free, community-maintained food database (~3.6 M products) covering
many countries and languages. Use these tools when you have an EAN/UPC barcode
and want macros — much more precise than name search. Note that submitting
custom ingredients to wger from the MCP is not supported — wger's REST
`/ingredient/` is read-only, and the old web-form path was dropped with the
move to multi-user auth.

Localisation: OFF stores per-language fields (``product_name_<lang>``,
``ingredients_text_<lang>``). Which one is requested and preferred comes from
``DEFAULT_LANGUAGE`` (default ``en``), overridable per call via the tools'
``language`` argument. The language-neutral ``product_name`` is the fallback.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..config import Settings

_OFF_BASE_URL = "https://world.openfoodfacts.org"
_OFF_TIMEOUT = 15.0
_BATCH_CONCURRENCY = 4  # OFF burst-limits aggressively; keep modest
_RETRY_429_DELAY = 2.0  # seconds before single retry on rate-limit

# Language-neutral fields, always requested.
_BASE_FIELDS = (
    "code",
    "product_name",
    "brands",
    "quantity",
    "countries_tags",
    "nutriscore_grade",
    "nova_group",
    "nutriments",
)
# Per-language OFF field templates, filled with the resolved language code.
_LOCALISED_FIELDS = ("product_name_{lang}", "ingredients_text_{lang}")


def _fields_for(lang: str) -> str:
    """The OFF ``fields=`` value for a given ISO 639-1 language code.

    Requesting a language OFF has no data for is safe: the response is still
    200 and simply omits the key, so no validation against a language list is
    needed here. Note OFF often returns ``""`` rather than omitting a
    per-language field — ``_shape`` treats both as absent.
    """
    localised = [tpl.format(lang=lang) for tpl in _LOCALISED_FIELDS]
    return ",".join([*_BASE_FIELDS, *localised])


def _err(status: int, detail: Any) -> dict[str, Any]:
    """Shape an OFF failure as a tool-response dict.

    Deliberately not ``common.api_err``: that one speaks for the wger API and
    would report an unreachable Open Food Facts as "wger is unreachable".
    """
    return {"error": True, "status": status, "detail": detail}


def _f(nut: dict[str, Any], *keys: str) -> float | None:
    """First non-null float value across the given OFF nutriment keys."""
    for k in keys:
        v = nut.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _scalar(v: Any) -> Any | None:
    """Normalise an OFF text field to a single value.

    OFF occasionally returns a list where a string is expected; take the first
    entry. Empty strings and empty lists collapse to ``None`` so callers can
    treat "absent" and "blank" alike (OFF commonly returns ``""`` for a
    language it has no data for).
    """
    if isinstance(v, list):
        v = v[0] if v else None
    return v or None


def _shape(prod: dict[str, Any], lang: str) -> dict[str, Any]:
    """Flatten an OFF product into a wger-aware structure.

    ``lang`` selects which localised OFF fields are preferred; the
    language-neutral ``product_name`` is the fallback.
    """
    nut = prod.get("nutriments") or {}
    name_key = f"product_name_{lang}"
    ingredients_key = f"ingredients_text_{lang}"

    energy = _f(nut, "energy-kcal_100g", "energy-kcal")
    protein = _f(nut, "proteins_100g")
    carbs = _f(nut, "carbohydrates_100g")
    sugars = _f(nut, "sugars_100g")
    fat = _f(nut, "fat_100g")
    fat_sat = _f(nut, "saturated-fat_100g")
    fiber = _f(nut, "fiber_100g")
    salt = _f(nut, "salt_100g")
    sodium = _f(nut, "sodium_100g")
    # OFF stores salt; wger stores sodium. Convert if sodium is missing.
    if sodium is None and salt is not None:
        sodium = round(salt / 2.5, 4)

    localized_name = _scalar(prod.get(name_key))
    default_name = _scalar(prod.get("product_name"))
    name = localized_name or default_name
    brand = _scalar(prod.get("brands"))

    macros_per_100g = {
        "energy_kcal": energy,
        "protein_g": protein,
        "carbohydrates_g": carbs,
        "carbohydrates_sugar_g": sugars,
        "fat_g": fat,
        "fat_saturated_g": fat_sat,
        "fiber_g": fiber,
        "salt_g": salt,
        "sodium_g": sodium,
    }

    return {
        "found": True,
        "code": prod.get("code"),
        "name": name,
        "language": lang,
        "name_localized": localized_name,
        "name_default": default_name,
        "brand": brand,
        "quantity": prod.get("quantity"),
        "countries": prod.get("countries_tags"),
        "ingredients_text": _scalar(prod.get(ingredients_key)),
        "nutriscore_grade": prod.get("nutriscore_grade"),
        "nova_group": prod.get("nova_group"),
        "macros_per_100g": macros_per_100g,
        "source": "openfoodfacts.org",
    }


def build_http() -> httpx.AsyncClient:
    """The OFF client; the caller owns and closes it."""
    return httpx.AsyncClient(
        base_url=_OFF_BASE_URL,
        timeout=_OFF_TIMEOUT,
        headers={"User-Agent": "wger-mcp/0.1 (+OFF-lookup)"},
    )


def register(mcp: FastMCP, http: httpx.AsyncClient, settings: Settings) -> None:
    default_language = settings.default_language

    async def _fetch_one(code: str, lang: str) -> dict[str, Any]:
        """One barcode fetch with a single retry on 429 (rate limit).

        The one path to OFF, for the single lookup and the batch alike: they
        asked the same question of the same endpoint, so a retry that only one
        of them had was a difference nobody chose.
        """
        for attempt in (1, 2):
            try:
                resp = await http.get(
                    f"/api/v2/product/{code}.json", params={"fields": _fields_for(lang)}
                )
            except httpx.HTTPError as exc:
                return _err(503, f"OFF unreachable: {exc}")
            if resp.status_code == 429 and attempt == 1:
                # Respect server's Retry-After if present, else our default.
                try:
                    delay = float(resp.headers.get("retry-after") or _RETRY_429_DELAY)
                except ValueError:
                    delay = _RETRY_429_DELAY
                await asyncio.sleep(min(delay, 10.0))
                continue
            if resp.status_code >= 400:
                return _err(resp.status_code, resp.text[:200])
            try:
                data = resp.json()
            except ValueError:
                return _err(502, "non-JSON response from OFF")
            if data.get("status") != 1:
                return {
                    "found": False,
                    "code": code,
                    "detail": data.get("status_verbose") or "product not found",
                }
            return _shape(data["product"], lang)
        return _err(429, "still rate-limited after retry")

    @mcp.tool()
    async def lookup_food_by_barcode(
        barcode: Annotated[str, Field(min_length=4, max_length=32)],
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}$")] = None,
    ) -> dict[str, Any]:
        """Look up an EAN/UPC/GTIN barcode on Open Food Facts.

        Returns the product's name, brand and macros per 100 g.

        ``language`` is an ISO 639-1 code ('en', 'pl', 'de', ...) selecting which
        localised OFF name/ingredients fields are preferred; it defaults to the
        server's ``DEFAULT_LANGUAGE``. The language-neutral ``product_name`` is
        the fallback when no localised name exists.

        Salt vs sodium: OFF stores salt only; if sodium is missing we derive
        ``sodium = salt / 2.5`` (the standard conversion).

        Not found → response includes a ``suggestion`` URL where you can add
        the product to OFF. After acceptance there it'll sync into your wger
        instance on the next ingredient-sync run.
        """
        result = await _fetch_one(barcode, language or default_language)
        if result.get("found") is False:
            result["suggestion"] = (
                "Not in Open Food Facts. You can add it at "
                f"https://world.openfoodfacts.org/cgi/product.pl?type=add&code={barcode} "
                "— community-moderated, free. After acceptance it syncs into wger."
            )
        return result

    @mcp.tool()
    async def lookup_foods_by_barcodes(
        barcodes: list[str],
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}$")] = None,
    ) -> dict[str, Any]:
        """Batch variant: look up many EANs at once. Returns a map keyed by
        barcode. Fetches happen concurrently (capped at 4 in flight) with a
        one-shot retry on 429.

        ``language`` works as in ``lookup_food_by_barcode`` and defaults to the
        server's ``DEFAULT_LANGUAGE``."""
        if not barcodes:
            return {"results": {}}
        lang = language or default_language
        # Deduplicate while preserving order.
        unique = list(dict.fromkeys(barcodes))
        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        async def _one(code: str) -> tuple[str, dict[str, Any]]:
            async with sem:
                return code, await _fetch_one(code, lang)

        results = dict(await asyncio.gather(*[_one(c) for c in unique]))
        return {"count": len(results), "results": results}
