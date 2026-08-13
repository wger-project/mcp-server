"""Exercise / ingredient catalog tools (read-only lookups)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..config import Settings
from ..wger_client import WgerClient, WgerError
from .common import bad_request, err

_NUTRISCORE = r"^[A-Ea-e]$"


# wger's search is one request per name. Filling a training day means a dozen
# of them, so the batch tool runs them concurrently — same cap as the Open Food
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


def register(mcp: FastMCP, client: WgerClient, settings: Settings) -> None:
    default_language = settings.default_language
    # exerciseinfo filters WHICH exercises match a language, but each one still
    # carries every translation. Picking the wrong one hands the caller a name in
    # a language it did not ask for, so resolve the code to wger's numeric id and
    # prefer that translation. Cached: the language table is static.
    _language_id: dict[str, int | None] = {}

    async def _language_id_for(code: str) -> int | None:
        if code not in _language_id:
            try:
                rows = await client.paginate(
                    "language/", params={"short_name": code}, limit=5
                )
                _language_id[code] = next(
                    (r.get("id") for r in rows if isinstance(r, dict) and r.get("id")), None
                )
            except WgerError:
                _language_id[code] = None
        return _language_id[code]

    async def _search(query: str, lang: str, limit: int) -> list[dict[str, Any]]:
        """One name-search, shaped down to what picks an exercise."""
        language_id = await _language_id_for(lang)
        # Fetch wider than asked for, rank locally, then trim: the extra rows cost
        # a little bandwidth and no context, and wger's own ordering buries the
        # exact match behind looser ones.
        results = await client.paginate(
            "exerciseinfo/",
            params={"name__search": query, "language__code": lang},
            limit=max(limit, _RANK_POOL),
        )
        q_lower = query.lower()
        shaped: list[dict[str, Any]] = []
        for ex in results:
            if not isinstance(ex, dict):
                continue
            translations = [
                t for t in (ex.get("translations") or []) if isinstance(t, dict) and t.get("name")
            ]
            in_language = [t for t in translations if t.get("language") == language_id]
            pool = in_language or translations
            match = next(
                (t for t in pool if q_lower in (t.get("name") or "").lower()),
                pool[0] if pool else None,
            )
            shaped.append({
                "id": ex.get("id"),
                "name": (match or {}).get("name"),
                "category": (ex.get("category") or {}).get("name"),
                "equipment": [e.get("name") for e in (ex.get("equipment") or [])],
            })
        shaped.sort(key=lambda s: _relevance(s.get("name") or "", query))
        return shaped[:limit]

    @mcp.tool()
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
        try:
            return await _search(query, language or default_language, limit)
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
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

        async def _one(q: str) -> tuple[str, Any]:
            async with sem:
                try:
                    return q, await _search(q, lang, limit_per_query)
                except WgerError as exc:
                    return q, [err(exc)]

        results = dict(await asyncio.gather(*[_one(q) for q in unique]))
        return {"count": len(results), "results": results}

    @mcp.tool()
    async def get_exercise(exercise_id: str) -> dict[str, Any]:
        """Fetch full exercise detail (instructions, muscles, equipment, images).

        Since wger 2.6 each image also carries ``thumbnails`` with ``small`` and
        ``medium`` URLs (returned verbatim in the raw detail)."""
        try:
            return await client.get(f"exerciseinfo/{exercise_id}/")
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
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
        params: dict[str, Any] = {
            "name__search": query,
            "language__code": language or default_language,
        }
        if nutriscore:
            params["nutriscore"] = nutriscore.upper()
        elif nutriscore_better_than:
            params["nutriscore__lt"] = nutriscore_better_than.upper()
        elif nutriscore_at_worst:
            params["nutriscore__lte"] = nutriscore_at_worst.upper()
        try:
            results = await client.paginate("ingredientinfo/", params=params, limit=limit)
        except WgerError as exc:
            return [err(exc)]
        shaped: list[dict[str, Any]] = []
        for ing in results:
            if not isinstance(ing, dict):
                continue
            shaped.append({
                "id": ing.get("id"),
                "uuid": ing.get("uuid"),
                "name": ing.get("name"),
                "energy": ing.get("energy"),
                "protein": ing.get("protein"),
                "carbohydrates": ing.get("carbohydrates"),
                "fat": ing.get("fat"),
                "brand": ing.get("brand"),
            })
        return shaped

    @mcp.tool()
    async def get_ingredient(ingredient_id: str) -> dict[str, Any]:
        """Fetch full ingredient detail (macros per 100 g, brand, etc.)."""
        try:
            return await client.get(f"ingredient/{ingredient_id}/")
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def search_ingredient_by_barcode(
        barcode: Annotated[str, Field(min_length=4, max_length=32)],
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> list[dict[str, Any]]:
        """Look up ingredients by EAN/UPC barcode (exact match on the wger
        `code` field). Typically returns 0 or 1 result — much more precise
        than name search."""
        try:
            results = await client.paginate(
                "ingredient/", params={"code": barcode}, limit=limit
            )
        except WgerError as exc:
            return [err(exc)]
        shaped: list[dict[str, Any]] = []
        for ing in results:
            if not isinstance(ing, dict):
                continue
            shaped.append({
                "id": ing.get("id"),
                "uuid": ing.get("uuid"),
                "name": ing.get("name"),
                "code": ing.get("code"),
                "brand": ing.get("brand"),
                "energy": ing.get("energy"),
                "protein": ing.get("protein"),
                "carbohydrates": ing.get("carbohydrates"),
                "fat": ing.get("fat"),
            })
        return shaped

    @mcp.tool()
    async def list_categories(
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List exercise categories (Chest, Back, …)."""
        try:
            return await client.paginate("exercisecategory/", limit=limit)
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
    async def list_muscles(
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List muscles."""
        try:
            return await client.paginate("muscle/", limit=limit)
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
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
        params: dict[str, Any] = {"language__code": language or default_language}
        if equipment_id is not None:
            params["equipment"] = equipment_id
        if muscle_id is not None:
            params["muscles"] = muscle_id
        if category_id is not None:
            params["category"] = category_id
        try:
            results = await client.paginate("exerciseinfo/", params=params, limit=limit)
        except WgerError as exc:
            return [err(exc)]
        language_id = await _language_id_for(params["language__code"])
        shaped: list[dict[str, Any]] = []
        for ex in results:
            if not isinstance(ex, dict):
                continue
            translations = [
                t for t in (ex.get("translations") or []) if isinstance(t, dict) and t.get("name")
            ]
            # Same rule as search_exercises: never hand back a name in a language
            # the caller did not ask for when one in that language exists.
            in_language = [t for t in translations if t.get("language") == language_id]
            pool = in_language or translations
            shaped.append({
                "id": ex.get("id"),
                "name": (pool[0].get("name") if pool else None),
                "category": (ex.get("category") or {}).get("name"),
                "equipment": [e.get("name") for e in (ex.get("equipment") or [])],
                "muscles": [m.get("name") for m in (ex.get("muscles") or [])],
            })
        return shaped
