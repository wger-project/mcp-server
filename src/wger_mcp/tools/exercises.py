"""Exercise / ingredient catalog tools (read-only lookups)."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..config import Settings
from ..wger_client import WgerClient, WgerError
from .common import bad_request, err

_NUTRISCORE = r"^[A-Ea-e]$"


def register(mcp: FastMCP, client: WgerClient, settings: Settings) -> None:
    default_language = settings.default_language

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
            results = await client.paginate(
                "exerciseinfo/",
                params={
                    "name__search": query,
                    "language__code": language or default_language,
                },
                limit=limit,
            )
        except WgerError as exc:
            return [err(exc)]
        q_lower = query.lower()
        shaped: list[dict[str, Any]] = []
        for ex in results:
            if not isinstance(ex, dict):
                continue
            translations = [
                t for t in (ex.get("translations") or []) if isinstance(t, dict) and t.get("name")
            ]
            match = next(
                (t for t in translations if q_lower in (t.get("name") or "").lower()),
                translations[0] if translations else None,
            )
            shaped.append({
                "id": ex.get("id"),
                "name": (match or {}).get("name"),
                "category": (ex.get("category") or {}).get("name"),
                "equipment": [e.get("name") for e in (ex.get("equipment") or [])],
            })
        return shaped

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
        shaped: list[dict[str, Any]] = []
        for ex in results:
            if not isinstance(ex, dict):
                continue
            translations = [
                t for t in (ex.get("translations") or []) if isinstance(t, dict) and t.get("name")
            ]
            shaped.append({
                "id": ex.get("id"),
                "name": (translations[0].get("name") if translations else None),
                "category": (ex.get("category") or {}).get("name"),
                "equipment": [e.get("name") for e in (ex.get("equipment") or [])],
                "muscles": [m.get("name") for m in (ex.get("muscles") or [])],
            })
        return shaped
