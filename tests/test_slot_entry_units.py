"""Unit fields on the routine-authoring tools take a name, not just wger's id."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import routines

ENTRY = api_models.SlotEntry(id=2, slot=1, exercise=73)


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _register() -> FastMCP:
    mcp = FastMCP("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )
    routines.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


class _Capture:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def body(self) -> Any:
        return self.calls[-1]["body"]


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


@pytest.mark.asyncio
async def test_attach_takes_a_unit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed hold is written as seconds, without the caller knowing wger's ids."""
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", create)
    await mcp.call_tool(
        "attach_exercise_to_slot",
        {
            "slot_id": "1",
            "exercise_id": "73",
            "repetition_unit": "seconds",
            "weight_unit": "lb",
        },
    )
    assert create.body.repetition_unit == 3
    assert create.body.weight_unit == 2


@pytest.mark.asyncio
async def test_update_takes_a_unit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    update = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_partial_update, "asyncio", update)
    await mcp.call_tool("update_slot_entry", {"slot_entry_id": "2", "repetition_unit": "seconds"})
    assert update.body.repetition_unit == 3


@pytest.mark.asyncio
async def test_numeric_ids_still_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing callers holding wger's own ids keep working unchanged."""
    mcp = _register()
    update = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_partial_update, "asyncio", update)
    await mcp.call_tool(
        "update_slot_entry",
        {"slot_entry_id": "2", "repetition_unit": 3, "weight_unit": 2},
    )
    assert update.body.repetition_unit == 3
    assert update.body.weight_unit == 2


@pytest.mark.asyncio
async def test_unknown_unit_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not fall through to wger as a silent wrong unit."""
    mcp = _register()
    update = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_partial_update, "asyncio", update)
    out = _result(
        await mcp.call_tool("update_slot_entry", {"slot_entry_id": "2", "repetition_unit": "secs"})
    )
    assert not update.called
    assert "secs" in json.dumps(out)
