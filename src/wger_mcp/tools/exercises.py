"""Exercise / ingredient catalog tools (read-only lookups), via the generated
``wger_api_client``."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client.api.exercisecategory import exercisecategory_list
from wger_api_client.api.exerciseinfo import exerciseinfo_list, exerciseinfo_retrieve
from wger_api_client.api.ingredient import ingredient_list, ingredient_retrieve
from wger_api_client.api.ingredientinfo import ingredientinfo_list
from wger_api_client.api.muscle import muscle_list
from wger_api_client.client import AuthenticatedClient
from wger_api_client.errors import UnexpectedStatus

from ..api_client import paginate
from ..config import Settings
from .common import (
    api_err,
    api_list_tool,
    api_tool,
    as_int,
    bad_request,
    language_id_resolver,
    opt,
)

# wger stores the grades lowercase, and so does the client's enum
_NUTRISCORE = r"^[A-Ea-e]$"


# wger's search is one request per name. Filling a training day means a dozen
# of them, so the batch tool runs them concurrently, same cap as the Open Food
# Facts batch lookup.
_BATCH_CONCURRENCY = 4

# How many candidates to pull before ranking locally. Bandwidth is cheap; the
# caller's context is not, so only the top few survive.
_RANK_POOL = 20


def _relevance(name: str, query: str) -> tuple[int, int]:
    """How well a name answers a query: matched words first, then brevity.

    wger's name__search matches any single word, so "incline barbell bench press"
    ranks a plain "Bench Press" alongside the incline variant. Counting how many
    of the query's words the name actually contains puts the specific match
    first; shorter names break ties, so "Bench Press" still beats "Bench Press
    Narrow Grip" for the query "bench press".
    """
    lowered = (name or "").lower()
    hits = sum(1 for word in query.lower().split() if word in lowered)
    return (-hits, len(lowered))


def _shape_ingredient(ing: dict[str, Any], *, with_code: bool = False) -> dict[str, Any]:
    # nutriscore earns its place because search_ingredients filters on it: a
    # search that can ask for "C or better" and then not say what it found
    # makes the caller fetch each result again to see. fiber is here because a
    # plan can set goal_fiber, so it is a number people pick foods by.
    out = {
        "id": ing.get("id"),
        "uuid": ing.get("uuid"),
        "name": ing.get("name"),
        "energy": ing.get("energy"),
        "protein": ing.get("protein"),
        "carbohydrates": ing.get("carbohydrates"),
        "fat": ing.get("fat"),
        "fiber": ing.get("fiber"),
        "nutriscore": ing.get("nutriscore"),
        "brand": ing.get("brand"),
    }
    if with_code:
        out["code"] = ing.get("code")
    return out


def _pick_name(ex: dict[str, Any], language_id: int | None, query: str | None) -> str | None:
    """The exercise's name in the requested language, preferring the query match.

    exerciseinfo filters WHICH exercises match a language, but each result still
    carries every translation, so without this a search answers in whatever
    language happens to come first.
    """
    translations = [
        t for t in (ex.get("translations") or []) if isinstance(t, dict) and t.get("name")
    ]
    pool = [t for t in translations if t.get("language") == language_id] or translations
    if query:
        q_lower = query.lower()
        match = next((t for t in pool if q_lower in (t.get("name") or "").lower()), None)
        if match:
            return match.get("name")
    return pool[0].get("name") if pool else None


def _shape_exercise(
    ex: dict[str, Any],
    language_id: int | None,
    query: str | None = None,
    *,
    with_muscles: bool = False,
) -> dict[str, Any]:
    """Enough to pick an exercise; get_exercise returns the full record."""
    shaped: dict[str, Any] = {
        "id": ex.get("id"),
        "name": _pick_name(ex, language_id, query),
        "category": (ex.get("category") or {}).get("name"),
        "equipment": [e.get("name") for e in (ex.get("equipment") or [])],
    }
    if with_muscles:
        shaped["muscles"] = [m.get("name") for m in (ex.get("muscles") or [])]
    return shaped


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    default_language = settings.default_language
    # exerciseinfo filters WHICH exercises match a language, but each one still
    # carries every translation. Picking the wrong one hands the caller a name in
    # a language it did not ask for, so resolve the code to wger's numeric id and
    # prefer that translation.
    _language_id_for = language_id_resolver(api)

    async def _search(query: str, lang: str, limit: int) -> list[dict[str, Any]]:
        """One name-search, shaped down to what picks an exercise."""
        language_id = await _language_id_for(lang)
        # Fetch wider than asked for, rank locally, then trim: the extra rows cost
        # a little bandwidth and no context, and wger's own ordering buries the
        # exact match behind looser ones.
        results = await paginate(
            exerciseinfo_list.asyncio,
            client=api,
            limit=max(limit, _RANK_POOL),
            name_search=query,
            language_code=lang,
        )
        shaped = [_shape_exercise(ex, language_id, query) for ex in results]
        shaped.sort(key=lambda s: _relevance(s.get("name") or "", query))
        return shaped[:limit]

    @mcp.tool()
    @api_list_tool
    async def search_exercises(
        query: Annotated[str, Field(min_length=2)],
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}$")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[dict[str, Any]]:
        """Search the wger exercise database by name.

        Returns id, name, category and equipment — enough to pick an exercise.
        Call get_exercise for images, translations and the full record.

        ``language`` is an ISO 639-1 code ('en', 'pl', 'de', ...); it defaults to
        the server's ``DEFAULT_LANGUAGE``.
        """
        return await _search(query, language or default_language, limit)

    @mcp.tool()
    @api_tool
    async def search_exercises_batch(
        queries: list[str],
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}$")] = None,
        limit_per_query: Annotated[int, Field(ge=1, le=10)] = 2,
    ) -> dict[str, Any]:
        """Batch variant: resolve many exercise names at once.

        Returns a map keyed by query, each holding the top matches in the same
        shape as search_exercises. Fetches run concurrently (capped at 4 in
        flight) and duplicate queries are collapsed.

        Use this when building or filling a training day. Searching one name per
        call costs an inference round trip per exercise, which is the difference
        between filling a day in one turn and running out of them.
        """
        if not queries:
            return {"count": 0, "results": {}}
        lang = language or default_language
        unique = list(dict.fromkeys(q for q in queries if q and len(q) >= 2))
        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        # One failing name is reported in its own entry, so the others still land
        async def _one(q: str) -> tuple[str, Any]:
            async with sem:
                try:
                    return q, await _search(q, lang, limit_per_query)
                except (UnexpectedStatus, httpx.HTTPError) as exc:
                    return q, [api_err(exc)]

        results = dict(await asyncio.gather(*[_one(q) for q in unique]))
        return {"count": len(results), "results": results}

    @mcp.tool()
    @api_tool
    async def get_exercise(exercise_id: str) -> dict[str, Any]:
        """Fetch full exercise detail (instructions, muscles, equipment, images).

        Since wger 2.6 each image also carries ``thumbnails`` with ``small`` and
        ``medium`` URLs (returned verbatim in the raw detail)."""
        exercise = await exerciseinfo_retrieve.asyncio(
            id=as_int(exercise_id, "exercise_id"), client=api
        )
        return exercise.to_dict()

    @mcp.tool()
    @api_list_tool
    async def search_ingredients(
        query: Annotated[str, Field(min_length=2)],
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}$")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        nutriscore: Annotated[str | None, Field(pattern=_NUTRISCORE)] = None,
        nutriscore_better_than: Annotated[str | None, Field(pattern=_NUTRISCORE)] = None,
        nutriscore_at_worst: Annotated[str | None, Field(pattern=_NUTRISCORE)] = None,
    ) -> list[dict[str, Any]]:
        """Search wger's ingredient database (foods with macros).

        Nutri-Score grades run A (best) → E (worst). Optional filters (wger 2.6):
        ``nutriscore`` exact grade; ``nutriscore_better_than='C'`` returns A/B
        only (strictly better); ``nutriscore_at_worst='C'`` returns A/B/C
        (C or better). Pass at most one of the three.

        ``language`` is an ISO 639-1 code; it defaults to the server's
        ``DEFAULT_LANGUAGE``.
        """
        chosen = [v for v in (nutriscore, nutriscore_better_than, nutriscore_at_worst) if v]
        if len(chosen) > 1:
            return [bad_request("pass at most one nutriscore filter")]
        results = await paginate(
            ingredientinfo_list.asyncio,
            client=api,
            limit=limit,
            name_search=query,
            language_code=language or default_language,
            nutriscore=opt(nutriscore.lower() if nutriscore else None),
            nutriscore_lt=opt(nutriscore_better_than.lower() if nutriscore_better_than else None),
            nutriscore_lte=opt(nutriscore_at_worst.lower() if nutriscore_at_worst else None),
        )
        return [_shape_ingredient(ing) for ing in results]

    @mcp.tool()
    @api_tool
    async def get_ingredient(ingredient_id: str) -> dict[str, Any]:
        """Fetch full ingredient detail (macros per 100 g, brand, etc.)."""
        ingredient = await ingredient_retrieve.asyncio(
            id=as_int(ingredient_id, "ingredient_id"), client=api
        )
        return ingredient.to_dict()

    @mcp.tool()
    @api_list_tool
    async def search_ingredient_by_barcode(
        barcode: Annotated[str, Field(min_length=4, max_length=32)],
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> list[dict[str, Any]]:
        """Look up ingredients by EAN/UPC barcode (exact match on the wger
        `code` field). Typically returns 0 or 1 result — much more precise
        than name search."""
        results = await paginate(ingredient_list.asyncio, client=api, limit=limit, code=barcode)
        return [_shape_ingredient(ing, with_code=True) for ing in results]

    @mcp.tool()
    @api_list_tool
    async def list_categories(
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List exercise categories (Chest, Back, …)."""
        return await paginate(exercisecategory_list.asyncio, client=api, limit=limit)

    @mcp.tool()
    @api_list_tool
    async def list_muscles(
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List muscles."""
        return await paginate(muscle_list.asyncio, client=api, limit=limit)

    @mcp.tool()
    @api_list_tool
    async def search_exercises_by_filter(
        equipment_id: str | None = None,
        muscle_id: str | None = None,
        category_id: str | None = None,
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}$")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Find exercises by structured filters (e.g. Dumbbell + Back).

        ``language`` is an ISO 639-1 code; it defaults to the server's
        ``DEFAULT_LANGUAGE``.
        """
        filters: dict[str, Any] = {}
        if equipment_id is not None:
            filters["equipment"] = as_int(equipment_id, "equipment_id")
        if muscle_id is not None:
            filters["muscles"] = as_int(muscle_id, "muscle_id")
        if category_id is not None:
            filters["category"] = as_int(category_id, "category_id")
        lang = language or default_language
        results = await paginate(
            exerciseinfo_list.asyncio,
            client=api,
            limit=limit,
            language_code=lang,
            **filters,
        )
        language_id = await _language_id_for(lang)
        return [_shape_exercise(ex, language_id, with_muscles=True) for ex in results]
