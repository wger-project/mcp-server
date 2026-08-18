"""Every field wger's WorkoutLog serializer accepts reaches the request.

The log carries more than reps and weight: the unit those reps are counted in,
the rest that followed, and the targets they were measured against. A field the
tool cannot send is one no later reading of the log can recover.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models
from wger_api_client.types import UNSET

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import workout_logs
from wger_mcp.tools.common import REPETITION_UNITS, as_decimal

LOG_ID = "018f6f30-0000-7000-8000-000000000003"
SESSION_ID = "018f6f30-0000-7000-8000-000000000009"
NEXT_LOG_ID = "018f6f30-0000-7000-8000-00000000000a"

LOG = api_models.WorkoutLog(exercise=73)


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
    workout_logs.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


def _creator(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    return create


def _patcher(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    patch = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_partial_update, "asyncio", patch)
    return patch


# ---------- the payload without the new arguments is unchanged ----------


@pytest.mark.asyncio
async def test_plain_set_sends_what_it_always_did(monkeypatch: pytest.MonkeyPatch) -> None:
    """New optional fields must not widen the payload of an ordinary set."""
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 8, "weight": 80})
    assert create.body.to_dict() == {
        "exercise": 73,
        "repetitions": "8",
        "weight": "80",
        "weight_unit": 1,
    }


# ---------- repetitions_unit ----------


@pytest.mark.asyncio
async def test_plank_is_logged_in_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the unit wger stores 60 repetitions, not a minute of work."""
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool(
        "log_set",
        {"exercise_id": "73", "reps": 60, "weight": 0, "reps_unit": "seconds"},
    )
    assert create.body.repetitions == "60"
    assert create.body.repetitions_unit == 3


@pytest.mark.asyncio
async def test_every_declared_repetition_unit_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    for name, unit_id in REPETITION_UNITS.items():
        await mcp.call_tool(
            "log_set", {"exercise_id": "73", "reps": 5, "weight": 0, "reps_unit": name}
        )
        assert create.body.repetitions_unit == unit_id


@pytest.mark.asyncio
async def test_unknown_repetition_unit_is_refused_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    out = _result(
        await mcp.call_tool(
            "log_set", {"exercise_id": "73", "reps": 5, "weight": 0, "reps_unit": "secs"}
        )
    )
    assert not create.calls
    message = json.dumps(out)
    assert "secs" in message
    assert "seconds" in message  # the error names the valid options


@pytest.mark.asyncio
async def test_a_run_keeps_its_fractional_distance(monkeypatch: pytest.MonkeyPatch) -> None:
    """reps is decimal(6, 2) in wger; 5.5 km must not be truncated to 5."""
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool(
        "log_set",
        {"exercise_id": "73", "reps": 5.5, "weight": 0, "reps_unit": "kilometers"},
    )
    assert create.body.repetitions == "5.5"


# ---------- rest, targets, session, dropset chain ----------


@pytest.mark.asyncio
async def test_planned_and_performed_travel_together(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool(
        "log_set",
        {
            "exercise_id": "73",
            "reps": 8,
            "weight": 80,
            "rir": 2,
            "rest": 120,
            "reps_target": 10,
            "weight_target": 82.5,
            "rir_target": 1,
            "rest_target": 180,
        },
    )
    body = create.body
    assert (body.repetitions, body.repetitions_target) == ("8", "10")
    assert (body.weight, body.weight_target) == ("80", "82.5")
    assert (body.rir, body.rir_target) == ("2", "1")
    assert (body.rest, body.rest_target) == (120, 180)


@pytest.mark.asyncio
async def test_set_attaches_to_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool(
        "log_set",
        {"exercise_id": "73", "reps": 8, "weight": 80, "session_id": SESSION_ID},
    )
    assert create.body.session == UUID(SESSION_ID)


@pytest.mark.asyncio
async def test_dropset_chains_to_the_next_log(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool(
        "log_set",
        {"exercise_id": "73", "reps": 8, "weight": 80, "next_log_id": NEXT_LOG_ID},
    )
    assert create.body.next_log == UUID(NEXT_LOG_ID)


@pytest.mark.asyncio
async def test_bad_session_id_is_refused_before_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    out = _result(
        await mcp.call_tool(
            "log_set", {"exercise_id": "73", "reps": 8, "weight": 80, "session_id": "42"}
        )
    )
    assert not create.calls
    assert "session_id" in json.dumps(out)


# ---------- RiR follows wger's own rule ----------


@pytest.mark.asyncio
async def test_rir_beyond_wgers_scale_is_refused_by_the_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wger validates RiR against RIR_OPTIONS, so 7 is a 400. Refusing it in
    the tool schema tells the caller why, instead of spending a round trip."""
    mcp = _register()
    create = _creator(monkeypatch)
    for field in ("rir", "rir_target"):
        with pytest.raises(Exception, match=r"4\.5"):
            await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 8, "weight": 80, field: 7})
    assert not create.calls


@pytest.mark.asyncio
async def test_rir_between_the_half_steps_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    with pytest.raises(Exception, match=r"0\.5"):
        await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 8, "weight": 80, "rir": 3.7})
    assert not create.calls


@pytest.mark.asyncio
async def test_every_valid_rir_option_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    for step in range(10):  # 0, 0.5, ... 4.5
        value = step / 2
        await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 8, "weight": 80, "rir": value})
        assert create.body.rir == as_decimal(value)


# ---------- patching ----------


@pytest.mark.asyncio
async def test_patch_sends_only_what_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    patch = _patcher(monkeypatch)
    await mcp.call_tool("update_workout_log", {"log_id": LOG_ID, "rest": 90})
    assert patch.body.to_dict() == {"rest": 90}


@pytest.mark.asyncio
async def test_patch_moves_a_set_to_the_right_exercise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the only fix for a mis-tapped exercise is delete and re-log."""
    mcp = _register()
    patch = _patcher(monkeypatch)
    await mcp.call_tool("update_workout_log", {"log_id": LOG_ID, "exercise_id": "145"})
    assert patch.body.exercise == 145
    assert patch.body.repetitions is UNSET
