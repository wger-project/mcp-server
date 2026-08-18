"""Measurement fields and filters the tools left out.

A body measurement is a small record — category, value, date, notes — which is
why the gaps here are about reaching all four rather than about any one of
them being complicated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import measurements

CATEGORY_ID = "018f6f30-0000-7000-8000-000000000001"
OTHER_CATEGORY_ID = "018f6f30-0000-7000-8000-000000000002"
ENTRY_ID = "018f6f30-0000-7000-8000-000000000003"

CATEGORY = api_models.Category(id=UUID(CATEGORY_ID), name="Waist", unit="cm")
ENTRY = api_models.Measurement(
    id=UUID(ENTRY_ID),
    category=UUID(CATEGORY_ID),
    value=82.5,
    date=datetime(2026, 8, 18, 12, tzinfo=UTC),
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
    measurements.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


# ---------- moving an entry between categories ----------


@pytest.mark.asyncio
async def test_entry_moves_to_another_category(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filed under Chest when it was a waist measurement: without this the only
    remedy is delete and re-log."""
    mcp = _register()
    patch = _Capture(ENTRY)
    monkeypatch.setattr(measurements.measurement_partial_update, "asyncio", patch)
    await mcp.call_tool(
        "update_measurement",
        {"measurement_id": ENTRY_ID, "category_id": OTHER_CATEGORY_ID},
    )
    assert patch.body.to_dict() == {"category": OTHER_CATEGORY_ID}


@pytest.mark.asyncio
async def test_a_bad_category_id_never_reaches_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    patch = _Capture(ENTRY)
    monkeypatch.setattr(measurements.measurement_partial_update, "asyncio", patch)
    out = _result(
        await mcp.call_tool("update_measurement", {"measurement_id": ENTRY_ID, "category_id": "7"})
    )
    assert not patch.calls
    assert "category_id" in json.dumps(out)


# ---------- date range ----------


@pytest.mark.asyncio
async def test_listing_scopes_to_a_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """date_to is inclusive, so it travels as the exclusive start of the day
    after — the same shape list_workout_logs uses."""
    listing = _Capture(api_models.PaginatedMeasurementList(count=1, results=[ENTRY]))
    monkeypatch.setattr(measurements.measurement_list, "asyncio", listing)
    mcp = _register()
    await mcp.call_tool(
        "list_measurements",
        {"category_id": CATEGORY_ID, "date_from": "2026-08-01", "date_to": "2026-08-18"},
    )
    call = listing.calls[-1]
    assert call["category"] == UUID(CATEGORY_ID)
    assert call["date_gte"] == datetime(2026, 8, 1, 0, 0)
    assert call["date_lt"] == datetime(2026, 8, 19, 0, 0)
    assert call["ordering"] == "-date"


@pytest.mark.asyncio
async def test_listing_without_a_range_is_unfiltered(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = _Capture(api_models.PaginatedMeasurementList(count=1, results=[ENTRY]))
    monkeypatch.setattr(measurements.measurement_list, "asyncio", listing)
    mcp = _register()
    await mcp.call_tool("list_measurements", {})
    call = listing.calls[-1]
    assert "date_gte" not in call
    assert "date_lt" not in call


# ---------- limits wger enforces ----------


@pytest.mark.asyncio
async def test_a_value_past_wgers_ceiling_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """MaxValueValidator(5000) on the model: 50000 is a 400, so the schema
    says so rather than spending the round trip."""
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(measurements.measurement_create, "asyncio", create)
    with pytest.raises(Exception, match="5000"):
        await mcp.call_tool("log_measurement", {"category_id": CATEGORY_ID, "value": 50000})
    assert not create.calls


@pytest.mark.asyncio
async def test_notes_longer_than_the_column_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(measurements.measurement_create, "asyncio", create)
    with pytest.raises(Exception, match="100"):
        await mcp.call_tool(
            "log_measurement",
            {"category_id": CATEGORY_ID, "value": 82.5, "notes": "x" * 101},
        )
    assert not create.calls


@pytest.mark.asyncio
async def test_a_category_rename_is_bounded_like_the_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_measurement_category bounded the name at 100 and the patch did
    not bound it at all."""
    mcp = _register()
    patch = _Capture(CATEGORY)
    monkeypatch.setattr(measurements.measurement_category_partial_update, "asyncio", patch)
    with pytest.raises(Exception, match="100"):
        await mcp.call_tool(
            "update_measurement_category",
            {"category_id": CATEGORY_ID, "name": "x" * 101},
        )
    assert not patch.calls


@pytest.mark.asyncio
async def test_an_ordinary_measurement_still_goes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(measurements.measurement_create, "asyncio", create)
    await mcp.call_tool(
        "log_measurement",
        {"category_id": CATEGORY_ID, "value": 82.5, "notes": "morning, fasted"},
    )
    assert create.body.to_dict() == {
        "category": CATEGORY_ID,
        "value": 82.5,
        "notes": "morning, fasted",
    }
