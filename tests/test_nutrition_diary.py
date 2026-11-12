"""Nutrition-diary timestamps: issue #5 — custom datetimes on log entries.

The wire format (URLs, serialisation, status codes) is the generated client's
contract, tested in the wger-api-client repo. Here we only verify that the
tools call the client with the right arguments.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from mcp.server.mcpserver import MCPServer
from wger_api_client import models as api_models
from wger_api_client.types import UNSET

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import nutrition

PLAN_ID = "018f6f30-0000-7000-8000-000000000001"
MEAL_ID = "018f6f30-0000-7000-8000-000000000002"
LOG_ID = "018f6f30-0000-7000-8000-000000000003"

LOG_ITEM = api_models.LogItem(plan=UUID(PLAN_ID), ingredient=1, amount="100")


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _register() -> MCPServer:
    mcp = MCPServer("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )
    api = build_api_client(settings, _StubProvider())
    nutrition.register(mcp, api, settings)
    return mcp


class _Capture:
    """Stands in for a generated endpoint function; records its kwargs."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result

    @property
    def body(self) -> Any:
        return self.calls[-1]["body"]


# ---------- log_ingredient ----------


@pytest.mark.asyncio
async def test_log_ingredient_forwards_iso_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for issue #5: an ISO 8601 date-time reaches the client intact.

    Previously every entry was pinned to a hard-coded 12:00Z regardless of
    what the caller passed.
    """
    mcp = _register()
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    await mcp.call_tool(
        "log_ingredient",
        {
            "plan_id": PLAN_ID,
            "ingredient_id": "1677954",
            "amount_g": 100,
            "when": "2026-07-21T07:00:00+02:00",
        },
    )
    tz = timezone(timedelta(hours=2))
    assert create.body.datetime_ == datetime(2026, 7, 21, 7, 0, tzinfo=tz)


@pytest.mark.asyncio
async def test_log_ingredient_omits_datetime_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No 'when' must send no datetime, so wger stamps the entry itself."""
    mcp = _register()
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    await mcp.call_tool(
        "log_ingredient",
        {"plan_id": PLAN_ID, "ingredient_id": "1", "amount_g": 50},
    )
    body = create.body
    assert body.datetime_ is UNSET
    assert body.plan == UUID(PLAN_ID)
    assert body.ingredient == 1
    assert body.amount == "50"
    assert body.meal is UNSET


@pytest.mark.asyncio
async def test_log_ingredient_accepts_bare_date(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    await mcp.call_tool(
        "log_ingredient",
        {"plan_id": PLAN_ID, "ingredient_id": "1", "amount_g": 10, "when": "2026-07-21"},
    )
    assert create.body.datetime_ == datetime(2026, 7, 21, 12, 0)


@pytest.mark.asyncio
async def test_log_ingredient_optional_meal(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    await mcp.call_tool(
        "log_ingredient",
        {"plan_id": PLAN_ID, "ingredient_id": "1", "amount_g": 10, "meal_id": MEAL_ID},
    )
    assert create.body.meal == UUID(MEAL_ID)


@pytest.mark.asyncio
async def test_log_ingredient_rejects_malformed_plan_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """An id the API cannot address is refused locally, without a request."""
    mcp = _register()
    create = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_create, "asyncio", create)
    result = await mcp.call_tool(
        "log_ingredient",
        {"plan_id": "not-a-uuid", "ingredient_id": "1", "amount_g": 10},
    )
    assert not create.calls
    assert "plan_id" in str(result)


# ---------- update_log_item ----------


@pytest.mark.asyncio
async def test_update_log_item_patches_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Correcting an entry's time in place — wger's web UI cannot do this."""
    mcp = _register()
    update = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_partial_update, "asyncio", update)
    await mcp.call_tool(
        "update_log_item",
        {"log_item_id": LOG_ID, "when": "2026-07-21T19:15:00+02:00"},
    )
    assert update.calls[-1]["id"] == UUID(LOG_ID)
    tz = timezone(timedelta(hours=2))
    assert update.body.datetime_ == datetime(2026, 7, 21, 19, 15, tzinfo=tz)
    assert update.body.amount is UNSET


@pytest.mark.asyncio
async def test_update_log_item_sends_only_given_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    update = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_partial_update, "asyncio", update)
    await mcp.call_tool("update_log_item", {"log_item_id": LOG_ID, "amount_g": 250})
    assert update.body.to_dict() == {"amount": "250"}


@pytest.mark.asyncio
async def test_update_log_item_rejects_empty_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    update = _Capture(LOG_ITEM)
    monkeypatch.setattr(nutrition.nutritiondiary_partial_update, "asyncio", update)
    result = await mcp.call_tool("update_log_item", {"log_item_id": LOG_ID})
    assert not update.calls
    assert "no fields to update" in str(result)
