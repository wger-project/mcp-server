"""What the tools return when an argument or the server is not cooperating."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models
from wger_api_client.errors import UnexpectedStatus

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import exercises, routines
from wger_mcp.tools.common import api_list_tool, api_tool


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )


def _register(module: Any) -> FastMCP:
    mcp = FastMCP("test")
    settings = _settings()
    module.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


def _raiser(exc: Exception):
    async def fn(**kwargs: Any) -> Any:
        raise exc

    return fn


# ---------- decorators ----------


@pytest.mark.asyncio
async def test_transport_failure_becomes_an_error_dict() -> None:
    """A wger that cannot be reached has no status code, so 503 stands in."""

    @api_tool
    async def tool() -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")

    out = await tool()
    assert out["error"] is True
    assert out["status"] == 503
    assert "unreachable" in out["detail"]


@pytest.mark.asyncio
async def test_list_tools_wrap_the_error_in_a_list() -> None:
    @api_list_tool
    async def tool() -> list[dict[str, Any]]:
        raise UnexpectedStatus(500, b"boom")

    out = await tool()
    assert isinstance(out, list)
    assert out[0]["status"] == 500


@pytest.mark.asyncio
async def test_a_response_parse_error_is_not_reported_as_a_bad_argument() -> None:
    """Only ToolInputError means "your argument was wrong"; a ValueError from
    parsing the response must not be relabelled as one."""

    @api_tool
    async def tool() -> dict[str, Any]:
        raise ValueError("Invalid isoformat string")

    with pytest.raises(ValueError):
        await tool()


# ---------- argument checks reach the caller as 400 ----------


@pytest.mark.asyncio
async def test_malformed_id_is_refused_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register(exercises)
    monkeypatch.setattr(
        exercises.exerciseinfo_retrieve, "asyncio", _raiser(AssertionError("called"))
    )
    out = _result(await mcp.call_tool("get_exercise", {"exercise_id": "not-a-number"}))
    assert out["status"] == 400
    assert "exercise_id" in out["detail"]


@pytest.mark.asyncio
async def test_fractional_value_for_a_whole_number_config_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sets and rest are whole numbers in wger; truncating 3.9 to 3 would
    report success for something the caller did not ask for."""
    mcp = _register(routines)
    monkeypatch.setattr(routines.sets_config_create, "asyncio", _raiser(AssertionError("called")))
    out = _result(
        await mcp.call_tool(
            "set_slot_entry_config", {"slot_entry_id": "7", "kind": "sets", "value": 3.9}
        )
    )
    assert out["status"] == 400
    assert "whole number" in out["detail"]


@pytest.mark.asyncio
async def test_rest_config_sends_whole_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """RestConfig.value is a PositiveIntegerField, not a decimal."""
    sent = {}

    async def capture(**kwargs: Any) -> Any:
        sent.update(kwargs)
        return api_models.RestConfig(id=1, slot_entry=7, iteration=1, value=90)

    mcp = _register(routines)
    monkeypatch.setattr(routines.rest_config_create, "asyncio", capture)
    await mcp.call_tool(
        "set_slot_entry_config", {"slot_entry_id": "7", "kind": "rest", "value": 90}
    )
    assert sent["body"].value == 90


@pytest.mark.asyncio
async def test_nutriscore_filters_are_sent_lowercase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wger stores the grades lowercase; an uppercase value is refused by the
    exact filter and sorts wrong in the comparisons."""
    sent = {}

    async def capture(**kwargs: Any) -> Any:
        sent.update(kwargs)
        return None

    mcp = _register(exercises)
    monkeypatch.setattr(exercises.ingredientinfo_list, "asyncio", capture)
    await mcp.call_tool("search_ingredients", {"query": "milk", "nutriscore_at_worst": "C"})
    assert sent["nutriscore_lte"] == "c"


# ---------- add_exercise_with_sets rollback ----------


@pytest.mark.asyncio
async def test_failed_exercise_attach_deletes_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry failure must not leave a slot that renders in no plan view."""
    deleted: list[int] = []

    async def _destroy(*, id: int, client: Any) -> Any:
        deleted.append(id)
        return None

    async def _slot(**kwargs: Any) -> Any:
        return api_models.Slot(id=42, day=8, order=1)

    monkeypatch.setattr(routines.slot_create, "asyncio", _slot)
    monkeypatch.setattr(
        routines.slot_entry_create, "asyncio", _raiser(UnexpectedStatus(400, b"no such exercise"))
    )
    monkeypatch.setattr(routines.slot_destroy, "asyncio_detailed", _destroy)

    mcp = _register(routines)
    out = _result(
        await mcp.call_tool(
            "add_exercise_with_sets",
            {"day_id": "8", "exercise_id": "999999", "sets": 3, "reps": 8},
        )
    )
    assert deleted == [42]
    assert out["error"] is True
    assert out["stage"] == "slot-entry"
    assert out["slot_rolled_back"] is True
    assert "slot" not in out


@pytest.mark.asyncio
async def test_rollback_failure_still_reports_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the cleanup delete also fails, the slot id stays in the response."""

    async def _slot(**kwargs: Any) -> Any:
        return api_models.Slot(id=42, day=8, order=1)

    monkeypatch.setattr(routines.slot_create, "asyncio", _slot)
    monkeypatch.setattr(
        routines.slot_entry_create, "asyncio", _raiser(UnexpectedStatus(400, b"nope"))
    )
    monkeypatch.setattr(
        routines.slot_destroy, "asyncio_detailed", _raiser(httpx.ConnectError("down"))
    )

    mcp = _register(routines)
    out = _result(
        await mcp.call_tool(
            "add_exercise_with_sets",
            {"day_id": "8", "exercise_id": "999999", "sets": 3, "reps": 8},
        )
    )
    assert out["slot_rolled_back"] is False
    assert out["slot"]["id"] == 42
