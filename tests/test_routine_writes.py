"""Routine and day creation against wger's actual field contract."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from wger_api_client import models as api_models

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import routines

ROUTINE = api_models.Routine(
    id=1,
    created=datetime(2026, 8, 24, tzinfo=UTC),
    start=date(2026, 8, 24),
    end=date(2026, 11, 16),
)
DAY = api_models.Day(id=5, routine=1)


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
    routines.register(mcp, api, settings)
    return mcp


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


def _result(raw: Any) -> Any:
    return raw.structured_content


# ---------- create_routine ----------


@pytest.mark.asyncio
async def test_end_is_always_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """wger marks end required; omitting it is a 400."""
    mcp = _register()
    create = _Capture(ROUTINE)
    monkeypatch.setattr(routines.routine_create, "asyncio", create)
    await mcp.call_tool("create_routine", {"name": "Recomp", "start": "2026-08-24"})
    assert create.body.start == date(2026, 8, 24)
    assert create.body.end == date(2026, 8, 24) + timedelta(weeks=12)


@pytest.mark.asyncio
async def test_explicit_end_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(ROUTINE)
    monkeypatch.setattr(routines.routine_create, "asyncio", create)
    await mcp.call_tool(
        "create_routine",
        {"name": "Recomp", "start": "2026-08-24", "end": "2026-11-14"},
    )
    assert create.body.end == date(2026, 11, 14)


@pytest.mark.asyncio
async def test_end_before_start_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(ROUTINE)
    monkeypatch.setattr(routines.routine_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "create_routine",
            {"name": "Recomp", "start": "2026-08-24", "end": "2026-08-01"},
        )
    )
    assert not create.calls
    assert "end" in json.dumps(out)


# ---------- add_routine_day ----------


@pytest.mark.asyncio
async def test_default_day_type_is_accepted_by_wger(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old default, "standard", is not in wger's DayType choices at all."""
    mcp = _register()
    create = _Capture(DAY)
    monkeypatch.setattr(routines.day_create, "asyncio", create)
    await mcp.call_tool("add_routine_day", {"routine_id": "1", "name": "Upper Push", "order": 1})
    assert create.body.type_ == "custom"
    assert create.body.type_ in routines.DAY_TYPES


@pytest.mark.asyncio
async def test_every_declared_day_type_is_sent_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _Capture(DAY)
    monkeypatch.setattr(routines.day_create, "asyncio", create)
    for day_type in routines.DAY_TYPES:
        await mcp.call_tool(
            "add_routine_day",
            {"routine_id": "1", "name": "D", "order": 1, "day_type": day_type},
        )
        assert create.body.type_ == day_type


@pytest.mark.asyncio
async def test_unknown_day_type_is_refused_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catch it here rather than spending a round trip on a 400 the model
    cannot read."""
    mcp = _register()
    create = _Capture(DAY)
    monkeypatch.setattr(routines.day_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "add_routine_day",
            {"routine_id": "1", "name": "Upper Push", "order": 1, "day_type": "standard"},
        )
    )
    assert not create.calls
    message = json.dumps(out)
    assert "standard" in message
    assert "custom" in message  # the error has to name the valid options


# ---------- config kinds ----------


@pytest.mark.asyncio
async def test_config_value_type_follows_the_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """sets wants an int, the decimal kinds want strings."""
    mcp = _register()
    sets_cfg = api_models.SetNrConfig(id=1, slot_entry=7, iteration=1, value=3)
    weight_cfg = api_models.WeightConfig(id=2, slot_entry=7, iteration=1, value="80")
    create_sets = _Capture(sets_cfg)
    create_weight = _Capture(weight_cfg)
    monkeypatch.setattr(routines.sets_config_create, "asyncio", create_sets)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", create_weight)

    await mcp.call_tool("set_slot_entry_config", {"slot_entry_id": "7", "kind": "sets", "value": 3})
    assert create_sets.body.value == 3

    await mcp.call_tool(
        "set_slot_entry_config", {"slot_entry_id": "7", "kind": "weight", "value": 82.5}
    )
    assert create_weight.body.value == "82.5"


@pytest.mark.asyncio
async def test_invalid_operation_is_refused_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """'a'/'s' from the old docstring never were valid wger operations."""
    mcp = _register()
    create = _Capture(None)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "set_slot_entry_config",
            {"slot_entry_id": "7", "kind": "weight", "value": 80, "operation": "a"},
        )
    )
    assert not create.calls
    assert "+" in json.dumps(out)
