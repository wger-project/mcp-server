"""Weight units (kg/lb) and RiR targets on logs and on planned sets."""

from __future__ import annotations

import json
from typing import Any

import respx
from mcp.server.fastmcp import FastMCP

from wger_mcp.config import Settings
from wger_mcp.tools import routines, workout_logs
from wger_mcp.wger_client import WgerClient

API = "https://wger.test/api/v2"


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
    module.register(mcp, WgerClient(API, _StubProvider()), _settings())
    return mcp


def _payload(route: Any) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


# ---------- log_set ----------


async def test_pounds_are_stored_as_pounds() -> None:
    """225 lb is recorded as 225 with unit lb, not silently converted to 102.06."""
    mcp = _register(workout_logs)
    with respx.mock(base_url=API) as mock:
        route = mock.post("/workoutlog/").respond(json={"id": "x"})
        await mcp.call_tool(
            "log_set",
            {"exercise_id": "73", "reps": 5, "weight": 225, "weight_unit": "lb", "rir": 2},
        )

    sent = _payload(route)
    assert sent["weight"] == 225
    assert sent["weight_unit"] == 2
    assert sent["rir"] == 2


async def test_kilograms_remain_the_default() -> None:
    mcp = _register(workout_logs)
    with respx.mock(base_url=API) as mock:
        route = mock.post("/workoutlog/").respond(json={"id": "x"})
        await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 5, "weight": 61.23})

    assert _payload(route)["weight_unit"] == 1


async def test_unknown_unit_is_refused() -> None:
    mcp = _register(workout_logs)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        route = mock.post("/workoutlog/").respond(json={"id": "x"})
        out = _result(
            await mcp.call_tool(
                "log_set",
                {"exercise_id": "73", "reps": 5, "weight": 100, "weight_unit": "pounds"},
            )
        )

    assert not route.called
    assert "pounds" in json.dumps(out)


async def test_update_leaves_unit_alone_when_not_given() -> None:
    mcp = _register(workout_logs)
    with respx.mock(base_url=API) as mock:
        route = mock.patch("/workoutlog/5/").respond(json={"id": 5})
        await mcp.call_tool("update_workout_log", {"log_id": "5", "reps": 6})

    assert _payload(route) == {"repetitions": 6}


# ---------- add_exercise_with_sets ----------


def _mock_exercise_creation(mock: Any) -> dict[str, Any]:
    routes = {
        "slot": mock.post("/slot/").respond(json={"id": 1}),
        "entry": mock.post("/slot-entry/").respond(json={"id": 2}),
        "sets": mock.post("/sets-config/").respond(json={"id": 3}),
        "reps": mock.post("/repetitions-config/").respond(json={"id": 4}),
        "weight": mock.post("/weight-config/").respond(json={"id": 5}),
        "rir": mock.post("/rir-config/").respond(json={"id": 6}),
    }
    return routes


async def test_planned_set_records_unit_and_rir() -> None:
    mcp = _register(routines)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        r = _mock_exercise_creation(mock)
        await mcp.call_tool(
            "add_exercise_with_sets",
            {
                "day_id": "8",
                "exercise_id": "73",
                "sets": 3,
                "reps": 8,
                "weight": 135,
                "weight_unit": "lb",
                "rir": 2,
            },
        )

    # The unit belongs to the slot entry, not to the weight config.
    assert _payload(r["entry"])["weight_unit"] == 2
    assert _payload(r["weight"])["value"] == 135
    assert r["rir"].called
    assert _payload(r["rir"])["value"] == 2


async def test_weight_may_be_omitted() -> None:
    """Prescribing sets and reps without inventing a load the coach cannot know."""
    mcp = _register(routines)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        r = _mock_exercise_creation(mock)
        await mcp.call_tool(
            "add_exercise_with_sets",
            {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 8, "rir": 2},
        )

    assert r["sets"].called and r["reps"].called
    assert not r["weight"].called
    assert r["rir"].called


async def test_rir_is_optional() -> None:
    mcp = _register(routines)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        r = _mock_exercise_creation(mock)
        await mcp.call_tool(
            "add_exercise_with_sets",
            {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 8, "weight": 60},
        )

    assert not r["rir"].called
    assert _payload(r["entry"])["weight_unit"] == 1


# ---------- set_slot_entry_config ----------


async def test_setting_a_weight_can_set_its_unit() -> None:
    """The unit lives on the entry, so setting a weight alone leaves it
    interpreted in whatever unit the entry already had."""
    mcp = _register(routines)
    with respx.mock(base_url=API) as mock:
        patch = mock.patch("/slot-entry/6/").respond(json={"id": 6})
        post = mock.post("/weight-config/").respond(json={"id": 9})
        await mcp.call_tool(
            "set_slot_entry_config",
            {"slot_entry_id": "6", "kind": "weight", "value": 175, "weight_unit": "lb"},
        )

    assert _payload(patch) == {"weight_unit": 2}
    assert _payload(post)["value"] == 175


async def test_unit_is_refused_for_a_non_weight_kind() -> None:
    mcp = _register(routines)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        patch = mock.patch("/slot-entry/6/").respond(json={"id": 6})
        post = mock.post("/repetitions-config/").respond(json={"id": 9})
        out = _result(
            await mcp.call_tool(
                "set_slot_entry_config",
                {"slot_entry_id": "6", "kind": "reps", "value": 10, "weight_unit": "lb"},
            )
        )

    assert not patch.called and not post.called
    assert "weight_unit" in json.dumps(out)


async def test_weight_without_unit_touches_only_the_config() -> None:
    mcp = _register(routines)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        patch = mock.patch("/slot-entry/6/").respond(json={"id": 6})
        post = mock.post("/weight-config/").respond(json={"id": 9})
        await mcp.call_tool(
            "set_slot_entry_config",
            {"slot_entry_id": "6", "kind": "weight", "value": 60},
        )

    assert not patch.called
    assert post.called
