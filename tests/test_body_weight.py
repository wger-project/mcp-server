"""Body weight after it stopped being its own endpoint.

wger 2.7 merged the weight table into measurements. These tools go through
`/api/v2/measurement/` rather than the `/weightentry/` shim, which is what lets
the unit travel with the reading instead of being whatever the profile said at
the moment of writing.
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
from wger_mcp.tools import body_weight

CATEGORY_ID = "018f6f30-0000-7000-8000-00000000000e"
ENTRY_ID = "018f6f30-0000-7000-8000-00000000000a"

CATEGORY = api_models.Category(
    id=UUID(CATEGORY_ID),
    name="Body weight",
    unit="kg",
    metric_type="body_weight",
    is_official=True,
)
ENTRY = api_models.Measurement(
    id=UUID(ENTRY_ID),
    category=UUID(CATEGORY_ID),
    value=80.0,
    date=datetime(2026, 8, 18, 12, tzinfo=UTC),
    extra_data={"unit": "kg"},
)


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


class _Capture:
    def __init__(self, *results: Any) -> None:
        self.results = list(results) or [None]
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]

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
    body_weight.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


def _rows(raw: Any) -> list[Any]:
    """The rows of a list-returning tool, which FastMCP wraps in `result`."""
    return _result(raw)["result"]


def _categories(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    listing = _Capture(api_models.PaginatedCategoryList(count=1, results=[CATEGORY]))
    monkeypatch.setattr(body_weight.measurement_category_list, "asyncio", listing)
    return listing


class _Profile:
    """Only to_dict() is read, and the real model wants eight more fields."""

    def __init__(self, unit: str) -> None:
        self.unit = unit

    def to_dict(self) -> dict[str, Any]:
        return {"weight_unit": self.unit}


def _profile(monkeypatch: pytest.MonkeyPatch, unit: str) -> _Capture:
    get = _Capture(_Profile(unit))
    monkeypatch.setattr(body_weight.userprofile_retrieve, "asyncio", get)
    return get


# ---------- the unit travels with the reading ----------


@pytest.mark.asyncio
async def test_an_explicit_unit_is_recorded_on_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of leaving the shim: 178 lb stays 178 lb whatever the
    profile is switched to afterwards."""
    _categories(monkeypatch)
    profile = _profile(monkeypatch, "kg")
    create = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.measurement_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool("log_body_weight", {"weight": 178, "unit": "lb"})
    assert create.body.extra_data == {"unit": "lb"}
    assert create.body.category == UUID(CATEGORY_ID)
    # An explicit unit needs no profile lookup
    assert not profile.calls


@pytest.mark.asyncio
async def test_without_a_unit_the_profile_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    _categories(monkeypatch)
    _profile(monkeypatch, "lb")
    create = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.measurement_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool("log_body_weight", {"weight": 178})
    assert create.body.extra_data == {"unit": "lb"}


@pytest.mark.asyncio
async def test_an_unknown_unit_never_reaches_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    _categories(monkeypatch)
    _profile(monkeypatch, "kg")
    create = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.measurement_create, "asyncio", create)
    mcp = _register()
    out = _result(await mcp.call_tool("log_body_weight", {"weight": 12.5, "unit": "stone"}))
    assert not create.calls
    assert "stone" in json.dumps(out)


@pytest.mark.asyncio
async def test_a_bare_date_lands_at_noon(monkeypatch: pytest.MonkeyPatch) -> None:
    _categories(monkeypatch)
    create = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.measurement_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool("log_body_weight", {"weight": 80, "unit": "kg", "when": "2026-08-18"})
    assert create.body.date == datetime(2026, 8, 18, 12, 0)


# ---------- reading a mixed history ----------


@pytest.mark.asyncio
async def test_history_reports_both_the_reading_and_its_kilograms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A history really can hold both units — the 2.7 backfill stamped one on
    every entry it carried over — so a comparison needs the normalised figure."""
    _categories(monkeypatch)
    entries = api_models.PaginatedMeasurementList(
        count=2,
        results=[
            api_models.Measurement(
                id=UUID(ENTRY_ID),
                category=UUID(CATEGORY_ID),
                value=178.0,
                extra_data={"unit": "lb"},
            ),
            ENTRY,
        ],
    )
    monkeypatch.setattr(body_weight.measurement_list, "asyncio", _Capture(entries))
    mcp = _register()
    rows = _rows(await mcp.call_tool("get_body_weight_history", {}))
    assert (rows[0]["weight"], rows[0]["unit"], rows[0]["weight_kg"]) == (178.0, "lb", 80.74)
    assert (rows[1]["weight"], rows[1]["unit"], rows[1]["weight_kg"]) == (80.0, "kg", 80.0)


@pytest.mark.asyncio
async def test_an_entry_without_a_unit_falls_back_to_the_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same fallback wger applies, so we do not invent a different answer."""
    _categories(monkeypatch)
    bare = api_models.Measurement(id=UUID(ENTRY_ID), category=UUID(CATEGORY_ID), value=75.0)
    entries = api_models.PaginatedMeasurementList(count=1, results=[bare])
    monkeypatch.setattr(body_weight.measurement_list, "asyncio", _Capture(entries))
    mcp = _register()
    rows = _rows(await mcp.call_tool("get_body_weight_history", {}))
    assert rows[0]["unit"] == "kg"


# ---------- patching ----------


@pytest.mark.asyncio
async def test_correcting_a_value_leaves_the_unit_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike the shim, which restamped the entry with the profile unit of the
    moment and so could change what the number meant."""
    patch = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.measurement_partial_update, "asyncio", patch)
    retrieve = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.measurement_retrieve, "asyncio", retrieve)
    mcp = _register()
    await mcp.call_tool("update_body_weight_entry", {"entry_id": ENTRY_ID, "weight": 79.5})
    assert patch.calls[-1]["id"] == UUID(ENTRY_ID)
    assert patch.body.to_dict() == {"value": 79.5}
    assert not retrieve.calls  # nothing to merge, so nothing to read


@pytest.mark.asyncio
async def test_restating_the_unit_keeps_the_import_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATCH replaces extra_data wholesale, so what the health importer wrote
    has to be read back and sent along."""
    synced = api_models.Measurement(
        id=UUID(ENTRY_ID),
        category=UUID(CATEGORY_ID),
        value=80.0,
        extra_data={"unit": "kg", "source_name": "Withings", "recording_method": "automatic"},
    )
    monkeypatch.setattr(body_weight.measurement_retrieve, "asyncio", _Capture(synced))
    patch = _Capture(synced)
    monkeypatch.setattr(body_weight.measurement_partial_update, "asyncio", patch)
    mcp = _register()
    await mcp.call_tool("update_body_weight_entry", {"entry_id": ENTRY_ID, "unit": "lb"})
    assert patch.body.extra_data == {
        "unit": "lb",
        "source_name": "Withings",
        "recording_method": "automatic",
    }


@pytest.mark.asyncio
async def test_an_empty_patch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.measurement_partial_update, "asyncio", patch)
    mcp = _register()
    out = _result(await mcp.call_tool("update_body_weight_entry", {"entry_id": ENTRY_ID}))
    assert not patch.calls
    assert "no fields to update" in json.dumps(out)


# ---------- ids ----------


@pytest.mark.asyncio
async def test_delete_sends_the_id_as_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    destroy = _Capture(None)
    monkeypatch.setattr(body_weight.measurement_destroy, "asyncio_detailed", destroy)
    mcp = _register()
    await mcp.call_tool("delete_body_weight_entry", {"entry_id": ENTRY_ID})
    assert destroy.calls[-1]["id"] == UUID(ENTRY_ID)


@pytest.mark.asyncio
async def test_an_integer_id_is_refused_up_front(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id every pre-2.7 caller holds."""
    destroy = _Capture(None)
    monkeypatch.setattr(body_weight.measurement_destroy, "asyncio_detailed", destroy)
    mcp = _register()
    out = _result(await mcp.call_tool("delete_body_weight_entry", {"entry_id": "42"}))
    assert not destroy.calls
    assert "entry_id" in json.dumps(out)
