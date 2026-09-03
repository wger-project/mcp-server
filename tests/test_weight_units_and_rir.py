"""Weight units (kg/lb) and RiR targets on logs and on planned sets."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models
from wger_api_client.types import UNSET

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import common, routines, workout_logs

LOG_ID = "018f6f30-0000-7000-8000-000000000003"

LOG = api_models.WorkoutLog(exercise=73)
SLOT = api_models.Slot(id=1, day=8)
ENTRY = api_models.SlotEntry(id=2, slot=1, exercise=73)


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


def _profile(monkeypatch: pytest.MonkeyPatch, unit: Any) -> None:
    """Stand in for /userprofile/, which decides the unit when none is passed."""

    async def _retrieve(**kwargs: Any) -> Any:
        return SimpleNamespace(weight_unit=unit)

    monkeypatch.setattr(common.userprofile_retrieve, "asyncio", _retrieve)


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


# ---------- log_set ----------


@pytest.mark.asyncio
async def test_pounds_are_stored_as_pounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """225 lb is recorded as 225 with unit lb, not silently converted to 102.06."""
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    await mcp.call_tool(
        "log_set",
        {"exercise_id": "73", "reps": 5, "weight": 225, "weight_unit": "lb", "rir": 2},
    )
    assert create.body.weight == "225"
    assert create.body.weight_unit == 2
    assert create.body.rir == "2"


@pytest.mark.asyncio
async def test_omitted_unit_follows_a_kilogram_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    _profile(monkeypatch, "kg")
    await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 5, "weight": 61.23})
    assert create.body.weight_unit == 1


@pytest.mark.asyncio
async def test_omitted_unit_follows_a_pound_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trainee whose profile says pounds reports pounds; 225 must not become 225 kg."""
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    _profile(monkeypatch, "lb")
    await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 5, "weight": 225})
    assert create.body.weight == "225"
    assert create.body.weight_unit == 2


@pytest.mark.asyncio
async def test_explicit_unit_beats_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    _profile(monkeypatch, "lb")
    await mcp.call_tool(
        "log_set",
        {"exercise_id": "73", "reps": 5, "weight": 100, "weight_unit": "kg"},
    )
    assert create.body.weight_unit == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("profile unreachable"),
        # from_dict raises TypeError for a unit outside {kg, lb}
        # (check_weight_unit_enum) and KeyError for a field it needs; neither is
        # a ValueError, which is why both are named.
        TypeError("Unexpected value 'stone'"),
        KeyError("username"),
    ],
    ids=["unreachable", "unknown-unit", "missing-field"],
)
async def test_unreadable_profile_refuses_the_write(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """A unit that cannot be read refuses the write instead of guessing kg.

    Standing in kg here would store a pounds trainee's 225 as 225 kg, and the
    row does not say which was meant, so no later read could undo it. The
    refusal costs one retry with an explicit weight_unit.
    """
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)

    async def _boom(**kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(common.userprofile_retrieve, "asyncio", _boom)
    out = _result(await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 5, "weight": 61.23}))
    assert not create.called
    assert json.loads(json.dumps(out))["error"] is True


@pytest.mark.asyncio
async def test_profile_without_a_unit_refuses_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile that parses but names no unit is no better a guess than an outage."""
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    _profile(monkeypatch, UNSET)
    out = _result(await mcp.call_tool("log_set", {"exercise_id": "73", "reps": 5, "weight": 61.23}))
    assert not create.called
    assert "weight_unit" in json.dumps(out)


@pytest.mark.asyncio
async def test_unknown_unit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(workout_logs)
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "log_set",
            {"exercise_id": "73", "reps": 5, "weight": 100, "weight_unit": "pounds"},
        )
    )
    assert not create.called
    assert "pounds" in json.dumps(out)


@pytest.mark.asyncio
async def test_update_leaves_unit_alone_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(workout_logs)
    update = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_partial_update, "asyncio", update)
    await mcp.call_tool("update_workout_log", {"log_id": LOG_ID, "reps": 6})
    assert update.body.to_dict() == {"repetitions": "6"}


# ---------- add_exercise_with_sets ----------


def _mock_creation(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Capture]:
    captures = {
        "slot": _Capture(SLOT),
        "entry": _Capture(ENTRY),
        "sets": _Capture(api_models.SetNrConfig(id=3, slot_entry=2, iteration=1, value="3")),
        "reps": _Capture(api_models.RepetitionsConfig(id=4, slot_entry=2, iteration=1, value="8")),
        "weight": _Capture(api_models.WeightConfig(id=5, slot_entry=2, iteration=1, value="135")),
        "rir": _Capture(api_models.RiRConfig(id=6, slot_entry=2, iteration=1, value="2")),
        "max_reps": _Capture(
            api_models.MaxRepetitionsConfig(id=7, slot_entry=2, iteration=1, value="12")
        ),
    }
    monkeypatch.setattr(routines.slot_create, "asyncio", captures["slot"])
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", captures["entry"])
    monkeypatch.setattr(routines.sets_config_create, "asyncio", captures["sets"])
    monkeypatch.setattr(routines.repetitions_config_create, "asyncio", captures["reps"])
    monkeypatch.setattr(routines.weight_config_create, "asyncio", captures["weight"])
    monkeypatch.setattr(routines.rir_config_create, "asyncio", captures["rir"])
    monkeypatch.setattr(routines.max_repetitions_config_create, "asyncio", captures["max_reps"])
    return captures


@pytest.mark.asyncio
async def test_planned_set_records_unit_and_rir(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(routines)
    c = _mock_creation(monkeypatch)
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
    assert c["entry"].body.weight_unit == 2
    assert c["weight"].body.value == "135"
    assert c["rir"].body.value == "2"


@pytest.mark.asyncio
async def test_planned_set_follows_a_pound_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting the unit takes it from the profile here too, not a hardcoded kg."""
    mcp = _register(routines)
    c = _mock_creation(monkeypatch)
    _profile(monkeypatch, "lb")
    await mcp.call_tool(
        "add_exercise_with_sets",
        {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 8, "weight": 225},
    )
    assert c["entry"].body.weight_unit == 2
    assert c["weight"].body.value == "225"


@pytest.mark.asyncio
async def test_weight_may_be_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prescribing sets and reps without inventing a load the coach cannot know."""
    mcp = _register(routines)
    c = _mock_creation(monkeypatch)
    await mcp.call_tool(
        "add_exercise_with_sets",
        {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 8, "rir": 2},
    )
    assert c["sets"].called and c["reps"].called
    assert not c["weight"].called
    assert c["rir"].called


@pytest.mark.asyncio
async def test_rir_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(routines)
    c = _mock_creation(monkeypatch)
    await mcp.call_tool(
        "add_exercise_with_sets",
        {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 8, "weight": 60},
    )
    assert not c["rir"].called
    assert c["entry"].body.weight_unit == 1


# ---------- set_slot_entry_config ----------


@pytest.mark.asyncio
async def test_setting_a_weight_can_set_its_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit lives on the entry, so setting a weight alone leaves it
    interpreted in whatever unit the entry already had."""
    mcp = _register(routines)
    patch = _Capture(ENTRY)
    post = _Capture(api_models.WeightConfig(id=9, slot_entry=6, iteration=1, value="175"))
    monkeypatch.setattr(routines.slot_entry_partial_update, "asyncio", patch)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", post)
    await mcp.call_tool(
        "set_slot_entry_config",
        {"slot_entry_id": "6", "kind": "weight", "value": 175, "weight_unit": "lb"},
    )
    assert patch.calls[-1]["id"] == 6
    assert patch.body.to_dict() == {"weight_unit": 2}
    assert post.body.value == "175"


@pytest.mark.asyncio
async def test_unit_is_refused_for_a_non_weight_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(routines)
    patch = _Capture(ENTRY)
    post = _Capture(api_models.RepetitionsConfig(id=9, slot_entry=6, iteration=1, value="10"))
    monkeypatch.setattr(routines.slot_entry_partial_update, "asyncio", patch)
    monkeypatch.setattr(routines.repetitions_config_create, "asyncio", post)
    out = _result(
        await mcp.call_tool(
            "set_slot_entry_config",
            {"slot_entry_id": "6", "kind": "reps", "value": 10, "weight_unit": "lb"},
        )
    )
    assert not patch.called and not post.called
    assert "weight_unit" in json.dumps(out)


@pytest.mark.asyncio
async def test_weight_without_unit_touches_only_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register(routines)
    patch = _Capture(ENTRY)
    post = _Capture(api_models.WeightConfig(id=9, slot_entry=6, iteration=1, value="60"))
    monkeypatch.setattr(routines.slot_entry_partial_update, "asyncio", patch)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", post)
    await mcp.call_tool(
        "set_slot_entry_config",
        {"slot_entry_id": "6", "kind": "weight", "value": 60},
    )
    assert not patch.called
    assert post.called
    assert post.body.to_dict().get("weight_unit", UNSET) is UNSET


# ---------- max_reps: reps as a range ----------


@pytest.mark.asyncio
async def test_max_reps_records_the_top_of_the_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "3 x 8-12" is two numbers; without max_reps the top lives only in the chat."""
    mcp = _register(routines)
    captures = _mock_creation(monkeypatch)
    await mcp.call_tool(
        "add_exercise_with_sets",
        {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 8, "max_reps": 12},
    )
    assert captures["reps"].body.value == "8"
    assert captures["max_reps"].body.value == "12"
    assert captures["max_reps"].body.slot_entry == 2


@pytest.mark.asyncio
async def test_max_reps_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(routines)
    captures = _mock_creation(monkeypatch)
    await mcp.call_tool(
        "add_exercise_with_sets",
        {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 8},
    )
    assert captures["reps"].called
    assert not captures["max_reps"].called


@pytest.mark.asyncio
async def test_max_reps_below_reps_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """reps is the BOTTOM of the range; a top beneath it is a caller mistake, not a plan."""
    mcp = _register(routines)
    captures = _mock_creation(monkeypatch)
    out = _result(
        await mcp.call_tool(
            "add_exercise_with_sets",
            {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 12, "max_reps": 8},
        )
    )
    assert not captures["slot"].called, "nothing may be written when the range is invalid"
    assert "max_reps" in json.dumps(out)


@pytest.mark.asyncio
async def test_max_reps_equal_to_reps_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """wger reports a range only where the top is strictly above the bottom.

    SetConfigData drops max_repetitions unless max_repetitions > repetitions,
    so an equal pair writes a config row that every later read of the plan
    discards. Refusing says that, where writing it would look like it worked.
    """
    mcp = _register(routines)
    captures = _mock_creation(monkeypatch)
    out = _result(
        await mcp.call_tool(
            "add_exercise_with_sets",
            {"day_id": "8", "exercise_id": "73", "sets": 3, "reps": 10, "max_reps": 10},
        )
    )
    assert not captures["slot"].called
    assert "max_reps" in json.dumps(out)


# ---------- attach_exercise_to_slot ----------


@pytest.mark.asyncio
async def test_attached_entry_follows_a_pound_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The composable path stamps the profile's unit too.

    Left unset, wger's own default applies (kg), so a pounds trainee building a
    plan this way had every weight later set on the entry read as kilograms.
    """
    mcp = _register(routines)
    create = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", create)
    _profile(monkeypatch, "lb")
    await mcp.call_tool("attach_exercise_to_slot", {"slot_id": "1", "exercise_id": "73"})
    assert create.body.weight_unit == 2


@pytest.mark.asyncio
async def test_attached_entry_keeps_an_explicit_unit_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller naming a unit wger has but this server does not (3 = plates) keeps it."""
    mcp = _register(routines)
    create = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", create)
    _profile(monkeypatch, "lb")
    await mcp.call_tool(
        "attach_exercise_to_slot",
        {"slot_id": "1", "exercise_id": "73", "weight_unit": 3},
    )
    assert create.body.weight_unit == 3
