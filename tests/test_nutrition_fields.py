"""Nutrition fields wger accepts that the tools never sent.

The one that matters most is the portion unit: wger scales a diary entry as
``amount x unit.gram``, and an entry logged in the app as two slices carries
the count, not the weight. Reading that count as grams under-reports the day.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models
from wger_api_client.types import UNSET

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import nutrition

PLAN_ID = "018f6f30-0000-7000-8000-000000000001"
LOG_ID = "018f6f30-0000-7000-8000-000000000003"

PLAN = api_models.NutritionPlan(id=UUID(PLAN_ID), creation_date=datetime(2026, 8, 18, tzinfo=UTC))
LOG_ITEM = api_models.LogItem(plan=UUID(PLAN_ID), ingredient=1, amount="100")

# A slice of this bread weighs 30 g — the number the summary has to find.
SLICE = api_models.IngredientWeightUnit(
    id=7, uuid=UUID("018f6f30-0000-7000-8000-00000000000b"), ingredient=1, gram=30, name="Slice"
)
BREAD = api_models.Ingredient(
    id=1,
    uuid=UUID("018f6f30-0000-7000-8000-00000000000c"),
    created=datetime(2026, 8, 18, tzinfo=UTC),
    last_update=datetime(2026, 8, 18, tzinfo=UTC),
    last_imported=datetime(2026, 8, 18, tzinfo=UTC),
    weight_units=[],
    language=2,
    name="Rye bread",
    energy=250,
    protein="9",
    carbohydrates="45",
    fat="3",
    fiber="6",
)


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


class _Capture:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result

    @property
    def body(self) -> Any:
        return self.calls[-1]["body"]


def _register() -> FastMCP:
    mcp = FastMCP("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )
    nutrition.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


def _entry(amount: str, weight_unit: int | None = None) -> api_models.LogItem:
    return api_models.LogItem(
        id=UUID(LOG_ID),
        plan=UUID(PLAN_ID),
        ingredient=1,
        amount=amount,
        weight_unit=weight_unit if weight_unit is not None else UNSET,
    )


def _diary(monkeypatch: pytest.MonkeyPatch, *entries: api_models.LogItem) -> None:
    async def _list(**_: Any) -> Any:
        return api_models.PaginatedLogItemList(count=len(entries), results=list(entries))

    monkeypatch.setattr(nutrition.nutritiondiary_list, "asyncio", _list)


# ---------- plan: dates and the fiber goal ----------


@pytest.mark.asyncio
async def test_plan_can_be_dated_and_take_a_fiber_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _Capture(PLAN)
    monkeypatch.setattr(nutrition.nutritionplan_create, "asyncio", create)
    await mcp.call_tool(
        "create_nutrition_plan",
        {
            "description": "Cut",
            "start": "2026-08-18",
            "end": "2026-10-13",
            "goal_fiber": 30,
        },
    )
    body = create.body
    assert (body.start, body.end) == (date(2026, 8, 18), date(2026, 10, 13))
    assert body.goal_fiber == 30


@pytest.mark.asyncio
async def test_plan_patch_sends_only_the_fiber_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    patch = _Capture(PLAN)
    monkeypatch.setattr(nutrition.nutritionplan_partial_update, "asyncio", patch)
    await mcp.call_tool("update_nutrition_plan", {"plan_id": PLAN_ID, "goal_fiber": 35})
    assert patch.body.to_dict() == {"goal_fiber": 35}


# ---------- portion units ----------


@pytest.mark.asyncio
async def test_units_are_listed_for_one_ingredient(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = _Capture(api_models.PaginatedIngredientWeightUnitList(count=1, results=[SLICE]))
    monkeypatch.setattr(nutrition.ingredientweightunit_list, "asyncio", listing)
    mcp = _register()
    await mcp.call_tool("list_ingredient_units", {"ingredient_id": "1"})
    assert listing.calls[-1]["ingredient"] == 1


@pytest.mark.asyncio
async def test_logging_a_portion_records_the_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    monkeypatch.setattr(nutrition.ingredientweightunit_retrieve, "asyncio", _Capture(SLICE))
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    await mcp.call_tool(
        "log_ingredient",
        {"plan_id": PLAN_ID, "ingredient_id": "1", "amount_g": 2, "weight_unit_id": "7"},
    )
    assert create.body.weight_unit == 7
    assert create.body.amount == "2"


@pytest.mark.asyncio
async def test_a_unit_from_another_ingredient_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wger's FK does not check the pairing; a slice-of-bread id on cheese
    would silently multiply every macro by 30."""
    mcp = _register()
    monkeypatch.setattr(nutrition.ingredientweightunit_retrieve, "asyncio", _Capture(SLICE))
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "log_ingredient",
            {"plan_id": PLAN_ID, "ingredient_id": "2", "amount_g": 2, "weight_unit_id": "7"},
        )
    )
    assert not create.calls
    message = json.dumps(out)
    assert "list_ingredient_units" in message


@pytest.mark.asyncio
async def test_plain_gram_logging_skips_the_unit_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check costs a request, so it must only run when a unit is given."""
    mcp = _register()
    lookup = _Capture(SLICE)
    monkeypatch.setattr(nutrition.ingredientweightunit_retrieve, "asyncio", lookup)
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    await mcp.call_tool(
        "log_ingredient", {"plan_id": PLAN_ID, "ingredient_id": "1", "amount_g": 100}
    )
    assert not lookup.calls
    assert create.body.to_dict() == {"plan": PLAN_ID, "ingredient": 1, "amount": "100"}


@pytest.mark.asyncio
async def test_patch_moves_an_entry_to_another_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    patch = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_partial_update, "asyncio", patch)
    await mcp.call_tool("update_log_item", {"log_item_id": LOG_ID, "plan_id": PLAN_ID})
    assert patch.body.to_dict() == {"plan": PLAN_ID}


# ---------- the summary reads portions as portions ----------


@pytest.mark.asyncio
async def test_two_slices_weigh_sixty_grams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this fixes: amount=2 with a 30 g unit counted as 2 g, so the
    day's intake came out at a thirtieth of the truth."""
    mcp = _register()
    _diary(monkeypatch, _entry("2", weight_unit=7))
    monkeypatch.setattr(nutrition.ingredient_retrieve, "asyncio", _Capture(BREAD))
    monkeypatch.setattr(nutrition.ingredientweightunit_retrieve, "asyncio", _Capture(SLICE))

    out = _result(await mcp.call_tool("nutrition_summary", {}))
    item = out["items"][0]
    assert item["amount_g"] == 60.0
    assert item["kcal"] == 150.0  # 250 kcal/100 g x 60 g
    assert item["logged_amount"] == 2.0
    assert item["logged_unit"] == "Slice"
    assert out["totals"]["kcal"] == 150.0


@pytest.mark.asyncio
async def test_gram_entries_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    _diary(monkeypatch, _entry("200"))
    lookup = _Capture(SLICE)
    monkeypatch.setattr(nutrition.ingredient_retrieve, "asyncio", _Capture(BREAD))
    monkeypatch.setattr(nutrition.ingredientweightunit_retrieve, "asyncio", lookup)

    out = _result(await mcp.call_tool("nutrition_summary", {}))
    assert not lookup.calls
    item = out["items"][0]
    assert item["amount_g"] == 200.0
    assert item["kcal"] == 500.0
    assert "logged_unit" not in item


@pytest.mark.asyncio
async def test_summary_reports_fiber(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan can set goal_fiber, so the summary has to report against it."""
    mcp = _register()
    _diary(monkeypatch, _entry("100"))
    monkeypatch.setattr(nutrition.ingredient_retrieve, "asyncio", _Capture(BREAD))

    out = _result(await mcp.call_tool("nutrition_summary", {}))
    assert out["totals"]["fiber_g"] == 6.0
    assert out["items"][0]["fiber_g"] == 6.0


@pytest.mark.asyncio
async def test_an_unreadable_unit_is_not_taken_for_grams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to grams is exactly the miscount the lookup prevents, so a
    failed one has to be reported instead of guessed around."""
    mcp = _register()
    _diary(monkeypatch, _entry("2", weight_unit=7))
    monkeypatch.setattr(nutrition.ingredient_retrieve, "asyncio", _Capture(BREAD))

    async def _boom(**_: Any) -> Any:
        raise nutrition.UnexpectedStatus(500, b"nope")

    monkeypatch.setattr(nutrition.ingredientweightunit_retrieve, "asyncio", _boom)

    out = _result(await mcp.call_tool("nutrition_summary", {}))
    assert "error" in out["items"][0]
    assert out["totals"]["kcal"] == 0.0
