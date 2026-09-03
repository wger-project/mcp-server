"""Attaching logged sets to a routine: get_workout_for_date + log_set linkage."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models
from wger_api_client.types import UNSET

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import common, routines, workout_logs

TODAY = date.today()

LOG = api_models.WorkoutLog(exercise=73)


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _register(module: Any) -> FastMCP:
    mcp = FastMCP("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )
    module.register(mcp, build_api_client(settings, _StubProvider()), settings)
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
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def body(self) -> Any:
        return self.calls[-1]["body"]


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


def _set_config(**overrides: Any) -> dict[str, Any]:
    """One planned set, with every field the gym-mode serializer emits.

    Written out in full on purpose: the payload is parsed by the generated
    client, so a fixture that skips fields would stop matching what wger
    actually sends without anything failing here.
    """
    cfg = {
        "slot_entry_id": 501,
        "exercise": 73,
        "sets": 1,
        "max_sets": None,
        "weight": "61.23",
        "max_weight": "None",
        "weight_unit": 1,
        "weight_rounding": "1.25",
        "repetitions": "5",
        "max_repetitions": "None",
        "repetitions_unit": 1,
        "repetitions_rounding": "1.00",
        "rir": "2",
        "max_rir": "None",
        "rpe": "8",
        "rest": "180",
        "max_rest": "None",
        "type": "normal",
        "text_repr": "5 @ 61.23 kg",
        "comment": "",
    }
    cfg.update(overrides)
    return cfg


def _sequence(
    day_date: date = TODAY,
    *,
    is_rest: bool = False,
    sets: list[dict[str, Any]] | None = None,
) -> list[api_models.WorkoutDayDataGymMode]:
    """A date-sequence-gym payload, parsed exactly as the client parses it."""
    slots = (
        []
        if sets == []
        else [
            {
                "comment": "",
                "is_superset": False,
                "exercises": [73],
                "sets": sets if sets is not None else [_set_config()],
            }
        ]
    )
    return [
        api_models.WorkoutDayDataGymMode.from_dict(
            {
                "iteration": 3,
                "date": day_date.isoformat(),
                "label": "Week 3",
                "day": {
                    "id": 11,
                    "routine": 7,
                    "name": "Push",
                    "is_rest": is_rest,
                    "description": "Working reps = lower end of range: bench 6-8.",
                },
                "slots": slots,
            }
        )
    ]


def _rows(items: list[dict[str, Any]]) -> Any:
    """A paginated listing of plain rows, as ``paginate`` consumes it."""

    async def fn(**kwargs: Any) -> Any:
        return SimpleNamespace(
            count=len(items),
            results=[SimpleNamespace(to_dict=lambda row=row: row) for row in items],
        )

    return fn


def _mock_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names live on translations; the plan endpoints carry only ids."""
    monkeypatch.setattr(
        common.language_list,
        "asyncio",
        _rows([{"id": 2, "short_name": "en", "full_name": "English"}]),
    )
    monkeypatch.setattr(
        routines.exercise_translation_list,
        "asyncio",
        _rows(
            [
                {"exercise": 73, "language": 1, "name": "Bankdrücken"},
                {"exercise": 73, "language": 2, "name": "Bench Press"},
            ]
        ),
    )


# ---------- get_workout_for_date ----------


@pytest.mark.asyncio
async def test_returns_slot_entry_ids_for_today(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(routines)
    monkeypatch.setattr(routines.routine_date_sequence_gym_list, "asyncio", _Capture(_sequence()))
    _mock_names(monkeypatch)
    out = _result(await mcp.call_tool("get_workout_for_date", {"routine_id": "7"}))

    assert out["iteration"] == 3
    assert out["day_name"] == "Push"
    # The day's notes carry the terms the numbers were written under - a rep
    # range here - so a caller reporting the plan can quote them.
    assert out["day_description"] == "Working reps = lower end of range: bench 6-8."
    assert out["is_rest_day"] is False
    assert len(out["planned"]) == 1
    entry = out["planned"][0]
    # These three are what log_set needs; without them the linkage is guesswork.
    assert entry["slot_entry_id"] == 501
    assert entry["exercise_id"] == 73
    assert entry["repetitions"] == "5"
    # A name, not a bare id: reading the plan should not require a second lookup.
    assert entry["exercise_name"] == "Bench Press"
    # Never a bare id: a caller reading "1" will guess, and guess wrong.
    assert entry["weight_unit"] == "kg"


@pytest.mark.asyncio
async def test_gym_mode_lists_one_entry_per_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three planned sets are three entries, not one exercise with sets: 3."""
    mcp = _register(routines)
    monkeypatch.setattr(
        routines.routine_date_sequence_gym_list,
        "asyncio",
        _Capture(_sequence(sets=[_set_config() for _ in range(3)])),
    )
    _mock_names(monkeypatch)
    out = _result(await mcp.call_tool("get_workout_for_date", {"routine_id": "7"}))

    assert len(out["planned"]) == 3
    assert {e["slot_entry_id"] for e in out["planned"]} == {501}


@pytest.mark.asyncio
async def test_unknown_weight_unit_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """wger also has Body Weight, Plates, km/h: better a raw id than a wrong name."""
    mcp = _register(routines)
    monkeypatch.setattr(
        routines.routine_date_sequence_gym_list,
        "asyncio",
        _Capture(_sequence(sets=[_set_config(weight_unit=7)])),
    )
    _mock_names(monkeypatch)
    out = _result(await mcp.call_tool("get_workout_for_date", {"routine_id": "7"}))

    assert out["planned"][0]["weight_unit"] == 7


@pytest.mark.asyncio
async def test_unnamed_day_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A day need not be named; the unset name must not reach the tool boundary."""
    mcp = _register(routines)
    sequence = _sequence()
    sequence[0].day.name = UNSET
    sequence[0].day.description = UNSET
    monkeypatch.setattr(routines.routine_date_sequence_gym_list, "asyncio", _Capture(sequence))
    _mock_names(monkeypatch)
    out = _result(await mcp.call_tool("get_workout_for_date", {"routine_id": "7"}))

    assert out["day_name"] is None
    # Same treatment as the name: Unset must not survive the tool boundary.
    assert out["day_description"] is None
    assert len(out["planned"]) == 1


@pytest.mark.asyncio
async def test_date_outside_the_routine_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A date the routine does not cover reports a rest day, not a failure."""
    mcp = _register(routines)
    monkeypatch.setattr(
        routines.routine_date_sequence_gym_list,
        "asyncio",
        _Capture(_sequence(date(1999, 1, 1))),
    )
    out = _result(await mcp.call_tool("get_workout_for_date", {"routine_id": "7"}))

    assert out["is_rest_day"] is True
    assert out["planned"] == []
    assert out["iteration"] is None
    assert "note" in out
    # Every key the scheduled-day answer carries is present here too, so a
    # caller reading the payload does not have to branch on which it got.
    assert out["day_description"] is None


@pytest.mark.asyncio
async def test_rest_day_reports_no_planned_work(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(routines)
    monkeypatch.setattr(
        routines.routine_date_sequence_gym_list,
        "asyncio",
        _Capture(_sequence(is_rest=True, sets=[])),
    )
    out = _result(await mcp.call_tool("get_workout_for_date", {"routine_id": "7"}))

    assert out["is_rest_day"] is True
    assert out["planned"] == []
    assert out["day_name"] == "Push"


# ---------- log_set linkage ----------


@pytest.mark.asyncio
async def test_log_set_attaches_to_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    await mcp.call_tool(
        "log_set",
        {
            "exercise_id": "73",
            "reps": 5,
            "weight": 61.23,
            "routine_id": "7",
            "slot_entry_id": "501",
            "iteration": 3,
        },
    )

    sent = create.body.to_dict()
    # Ints, not the strings the tool boundary takes: wger rejects a string here.
    assert sent["routine"] == 7
    assert sent["slot_entry"] == 501
    assert sent["iteration"] == 3


@pytest.mark.asyncio
async def test_log_set_without_linkage_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freestanding logging must keep working exactly as before."""
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 5, "weight": 61.23})

    assert set(create.body.to_dict()) == {"exercise", "repetitions", "weight", "weight_unit"}


@pytest.mark.asyncio
async def test_slot_entry_without_routine_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """wger ties a slot entry to its routine; sending one alone logs it wrong."""
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "log_set",
            {"exercise_id": "73", "reps": 5, "weight": 61.23, "slot_entry_id": "501"},
        )
    )

    assert not create.called
    assert "routine_id" in json.dumps(out)
