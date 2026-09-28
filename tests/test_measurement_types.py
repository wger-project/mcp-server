"""What wger 2.7's typed measurements made reachable.

Categories carry a metric_type now, entries carry where they came from, the
server can condense a series itself, and a blood pressure is two rows that only
mean something together.
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

GROUP_ID = "018f6f30-0000-7000-8000-00000000000b"
SYS_ID = "018f6f30-0000-7000-8000-00000000000c"
DIA_ID = "018f6f30-0000-7000-8000-00000000000d"
ENTRY_ID = "018f6f30-0000-7000-8000-000000000003"


def _category(cid: str, name: str, metric_type: str, parent: str | None = None):
    return api_models.Category(
        id=UUID(cid),
        name=name,
        unit="mmHg",
        metric_type=metric_type,
        is_official=False,
        parent=UUID(parent) if parent else None,
    )


GROUP = _category(GROUP_ID, "Blood pressure", "blood_pressure")
SYS = _category(SYS_ID, "Systolic", "blood_pressure_systolic", GROUP_ID)
DIA = _category(DIA_ID, "Diastolic", "blood_pressure_diastolic", GROUP_ID)


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


class _Capture:
    """Returns each queued result in turn, repeating the last one."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        i = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[i]

    @property
    def body(self) -> Any:
        return self.calls[-1]["body"]

    @property
    def bodies(self) -> list[Any]:
        return [c["body"] for c in self.calls if "body" in c]


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


def _rows(raw: Any) -> list[Any]:
    """The rows of a list-returning tool, which FastMCP wraps in `result`."""
    return _result(raw)["result"]


def _page(*rows):
    return api_models.PaginatedCategoryList(count=len(rows), results=list(rows))


# ---------- typed categories ----------


@pytest.mark.asyncio
async def test_a_typed_category_gets_its_conventional_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'cm' is right for a bicep and wrong for a heart rate, and wger has no
    server-side default to fall back on."""
    create = _Capture(_category(SYS_ID, "Heart rate", "heart_rate"))
    monkeypatch.setattr(measurements.measurement_category_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool(
        "create_measurement_category", {"name": "Heart rate", "metric_type": "heart_rate"}
    )
    assert create.body.unit == "bpm"
    assert create.body.metric_type == "heart_rate"


@pytest.mark.asyncio
async def test_an_explicit_unit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    create = _Capture(_category(SYS_ID, "Height", "height"))
    monkeypatch.setattr(measurements.measurement_category_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool(
        "create_measurement_category",
        {"name": "Height", "metric_type": "height", "unit": "in"},
    )
    assert create.body.unit == "in"


@pytest.mark.asyncio
async def test_a_free_form_category_stays_untyped(monkeypatch: pytest.MonkeyPatch) -> None:
    create = _Capture(_category(SYS_ID, "Bicep", "custom"))
    monkeypatch.setattr(measurements.measurement_category_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool("create_measurement_category", {"name": "Bicep"})
    sent = create.body.to_dict()
    assert sent["unit"] == "cm"
    assert "metric_type" not in sent


# ---------- source ----------


@pytest.mark.asyncio
async def test_entries_can_be_narrowed_to_their_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = _Capture(api_models.PaginatedMeasurementList(count=0, results=[]))
    monkeypatch.setattr(measurements.measurement_list, "asyncio", listing)
    mcp = _register()
    await mcp.call_tool("list_measurements", {"source": "apple"})
    assert listing.calls[-1]["source"] == "apple"


@pytest.mark.asyncio
async def test_an_unknown_source_never_reaches_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = _Capture(api_models.PaginatedMeasurementList(count=0, results=[]))
    monkeypatch.setattr(measurements.measurement_list, "asyncio", listing)
    mcp = _register()
    out = _result(await mcp.call_tool("list_measurements", {"source": "fitbit"}))
    assert not listing.calls
    assert "fitbit" in json.dumps(out)


# ---------- aggregate ----------


@pytest.mark.asyncio
async def test_a_series_is_condensed_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint answers with the whole series at once, so paginate() and its
    `results` envelope do not apply."""
    bucket = api_models.Bucket(
        category=UUID(SYS_ID),
        start=datetime(2026, 8, 1, tzinfo=UTC),
        unit="kg",
        count=12,
        sum_="960.00",
        min_="79.00",
        max_="81.00",
    )
    agg = _Capture([bucket])
    monkeypatch.setattr(measurements.measurement_aggregate_list, "asyncio", agg)
    mcp = _register()
    out = _rows(
        await mcp.call_tool(
            "summarize_measurements",
            {"category_id": SYS_ID, "bucket": "month", "date_from": "2026-08-01"},
        )
    )
    call = agg.calls[-1]
    assert call["bucket"] == "month"
    assert call["category"] == UUID(SYS_ID)
    assert call["date_gte"] == datetime(2026, 8, 1, 0, 0)
    assert "limit" not in call and "offset" not in call
    assert out[0]["sum"] == "960.00"


@pytest.mark.asyncio
async def test_an_unknown_bucket_never_reaches_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    agg = _Capture([])
    monkeypatch.setattr(measurements.measurement_aggregate_list, "asyncio", agg)
    mcp = _register()
    out = _result(await mcp.call_tool("summarize_measurements", {"bucket": "fortnight"}))
    assert not agg.calls
    assert "fortnight" in json.dumps(out)


# ---------- blood pressure ----------


@pytest.mark.asyncio
async def test_a_reading_is_two_entries_with_one_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pairing *is* the identical timestamp. Letting the server default each
    would set them microseconds apart and they would stop being one reading."""
    cats = _Capture(_page(GROUP), _page(SYS, DIA))
    monkeypatch.setattr(measurements.measurement_category_list, "asyncio", cats)
    create = _Capture(api_models.Measurement(id=UUID(ENTRY_ID), category=UUID(SYS_ID), value=120))
    monkeypatch.setattr(measurements.measurement_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool(
        "log_blood_pressure",
        {"systolic": 120, "diastolic": 80, "when": "2026-08-18"},
    )
    sent = create.bodies
    assert len(sent) == 2
    assert sent[0].date == sent[1].date
    assert (sent[0].value, sent[1].value) == (120, 80)
    assert (sent[0].category, sent[1].category) == (UUID(SYS_ID), UUID(DIA_ID))


@pytest.mark.asyncio
async def test_the_group_is_built_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating the group creates its components, so a trainee who has never
    recorded one does not have to set anything up first."""
    cats = _Capture(_page(), _page(SYS, DIA))
    monkeypatch.setattr(measurements.measurement_category_list, "asyncio", cats)
    made = _Capture(GROUP)
    monkeypatch.setattr(measurements.measurement_category_create, "asyncio", made)
    create = _Capture(api_models.Measurement(id=UUID(ENTRY_ID), category=UUID(SYS_ID), value=120))
    monkeypatch.setattr(measurements.measurement_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool("log_blood_pressure", {"systolic": 118, "diastolic": 76})
    assert made.body.metric_type == "blood_pressure"
    assert made.body.unit == "mmHg"
    assert len(create.bodies) == 2


@pytest.mark.asyncio
async def test_swapped_numbers_are_refused_up_front(monkeypatch: pytest.MonkeyPatch) -> None:
    cats = _Capture(_page(GROUP), _page(SYS, DIA))
    monkeypatch.setattr(measurements.measurement_category_list, "asyncio", cats)
    create = _Capture(None)
    monkeypatch.setattr(measurements.measurement_create, "asyncio", create)
    mcp = _register()
    out = _result(await mcp.call_tool("log_blood_pressure", {"systolic": 80, "diastolic": 120}))
    assert not create.calls
    assert "diastolic" in json.dumps(out)


@pytest.mark.asyncio
async def test_history_pairs_the_two_halves_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cats = _Capture(_page(GROUP), _page(SYS, DIA))
    monkeypatch.setattr(measurements.measurement_category_list, "asyncio", cats)
    stamp = datetime(2026, 8, 18, 12, tzinfo=UTC)
    entries = api_models.PaginatedMeasurementList(
        count=2,
        results=[
            api_models.Measurement(id=UUID(ENTRY_ID), category=UUID(SYS_ID), value=120, date=stamp),
            api_models.Measurement(id=UUID(ENTRY_ID), category=UUID(DIA_ID), value=80, date=stamp),
        ],
    )
    listing = _Capture(entries)
    monkeypatch.setattr(measurements.measurement_list, "asyncio", listing)
    mcp = _register()
    rows = _rows(await mcp.call_tool("get_blood_pressure_history", {}))
    assert len(rows) == 1
    assert (rows[0]["systolic"], rows[0]["diastolic"]) == (120, 80)
