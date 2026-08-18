"""Fields of the routine tree that the tools used to leave out.

A planned set is more than sets x reps: what kind of set it is, whether the
plan may advance without logs, what a progression rounds to, and whether that
progression is earned. Each of these is a field wger has always accepted.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models
from wger_api_client.types import UNSET

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
SLOT = api_models.Slot(id=9, day=5)
ENTRY = api_models.SlotEntry(id=2, slot=9, exercise=73)
WEIGHT_CFG = api_models.WeightConfig(id=3, slot_entry=2, iteration=1, value="80")


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
    routines.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


# ---------- routine: template / public ----------


@pytest.mark.asyncio
async def test_routine_is_neither_template_nor_public_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _Capture(ROUTINE)
    monkeypatch.setattr(routines.routine_create, "asyncio", create)
    await mcp.call_tool("create_routine", {"name": "Recomp"})
    assert create.body.is_template is False
    assert create.body.is_public is False


@pytest.mark.asyncio
async def test_routine_can_be_published_as_a_template(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(ROUTINE)
    monkeypatch.setattr(routines.routine_create, "asyncio", create)
    await mcp.call_tool("create_routine", {"name": "5x5", "is_template": True, "is_public": True})
    assert (create.body.is_template, create.body.is_public) == (True, True)


@pytest.mark.asyncio
async def test_patching_sharing_leaves_the_rest_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    patch = _Capture(ROUTINE)
    monkeypatch.setattr(routines.routine_partial_update, "asyncio", patch)
    await mcp.call_tool("update_routine", {"routine_id": "1", "is_public": False})
    assert patch.body.to_dict() == {"is_public": False}


# ---------- day: need_logs_to_advance ----------


@pytest.mark.asyncio
async def test_day_advances_by_calendar_unless_asked_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _Capture(DAY)
    monkeypatch.setattr(routines.day_create, "asyncio", create)
    await mcp.call_tool("add_routine_day", {"routine_id": "1", "name": "Push", "order": 1})
    assert create.body.need_logs_to_advance is False


@pytest.mark.asyncio
async def test_day_can_wait_for_its_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missed session must not silently cost the trainee that day's work."""
    mcp = _register()
    create = _Capture(DAY)
    monkeypatch.setattr(routines.day_create, "asyncio", create)
    await mcp.call_tool(
        "add_routine_day",
        {"routine_id": "1", "name": "Push", "order": 1, "need_logs_to_advance": True},
    )
    assert create.body.need_logs_to_advance is True


@pytest.mark.asyncio
async def test_day_patch_carries_need_logs_to_advance(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    patch = _Capture(DAY)
    monkeypatch.setattr(routines.day_partial_update, "asyncio", patch)
    await mcp.call_tool("update_routine_day", {"day_id": "5", "need_logs_to_advance": True})
    assert patch.body.to_dict() == {"need_logs_to_advance": True}


# ---------- slot entry: type and rounding ----------


@pytest.mark.asyncio
async def test_entry_defaults_to_a_working_set(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", create)
    await mcp.call_tool("attach_exercise_to_slot", {"slot_id": "9", "exercise_id": "73"})
    assert create.body.type_ == "normal"


@pytest.mark.asyncio
async def test_every_declared_entry_type_is_sent_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warmup left at 'normal' counts as working volume ever after."""
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", create)
    for entry_type in routines.EXERCISE_TYPES:
        await mcp.call_tool(
            "attach_exercise_to_slot",
            {"slot_id": "9", "exercise_id": "73", "entry_type": entry_type},
        )
        assert create.body.type_ == entry_type


@pytest.mark.asyncio
async def test_unknown_entry_type_is_refused_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "attach_exercise_to_slot",
            {"slot_id": "9", "exercise_id": "73", "entry_type": "warm-up"},
        )
    )
    assert not create.calls
    message = json.dumps(out)
    assert "warm-up" in message
    assert "warmup" in message  # the error names the valid options


@pytest.mark.asyncio
async def test_rounding_travels_as_a_decimal_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a percentage step prescribes weights no bar can hold."""
    mcp = _register()
    create = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", create)
    await mcp.call_tool(
        "attach_exercise_to_slot",
        {
            "slot_id": "9",
            "exercise_id": "73",
            "weight_rounding": 2.5,
            "repetition_rounding": 1,
        },
    )
    assert create.body.weight_rounding == "2.5"
    assert create.body.repetition_rounding == "1"


@pytest.mark.asyncio
async def test_convenience_tool_can_plan_a_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    monkeypatch.setattr(routines.slot_create, "asyncio", _Capture(SLOT))
    entry = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_create, "asyncio", entry)
    monkeypatch.setattr(
        routines.sets_config_create,
        "asyncio",
        _Capture(api_models.SetNrConfig(id=1, slot_entry=2, iteration=1, value=3)),
    )
    monkeypatch.setattr(
        routines.repetitions_config_create,
        "asyncio",
        _Capture(api_models.RepetitionsConfig(id=2, slot_entry=2, iteration=1, value="10")),
    )
    await mcp.call_tool(
        "add_exercise_with_sets",
        {
            "day_id": "5",
            "exercise_id": "73",
            "sets": 3,
            "reps": 10,
            "entry_type": "warmup",
        },
    )
    assert entry.body.type_ == "warmup"


# ---------- moving slots and entries ----------


@pytest.mark.asyncio
async def test_slot_moves_to_another_day(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    patch = _Capture(SLOT)
    monkeypatch.setattr(routines.slot_partial_update, "asyncio", patch)
    await mcp.call_tool("update_slot", {"slot_id": "9", "day_id": "6"})
    assert patch.body.to_dict() == {"day": 6}


@pytest.mark.asyncio
async def test_entry_moves_to_another_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    patch = _Capture(ENTRY)
    monkeypatch.setattr(routines.slot_entry_partial_update, "asyncio", patch)
    await mcp.call_tool("update_slot_entry", {"slot_entry_id": "2", "slot_id": "11"})
    assert patch.body.to_dict() == {"slot": 11}


# ---------- RiR configs follow wger's own rule ----------


@pytest.mark.asyncio
async def test_rir_config_off_the_half_step_is_refused_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RiRConfig.value carries validate_rir too, so the plan side needs the
    same bound as the log side."""
    mcp = _register()
    for kind in ("rir", "max_rir"):
        create = _Capture(WEIGHT_CFG)
        monkeypatch.setattr(routines.CONFIG_KINDS[kind].create_mod, "asyncio", create)
        for value in (3.7, 5):
            out = _result(
                await mcp.call_tool(
                    "set_slot_entry_config",
                    {"slot_entry_id": "2", "kind": kind, "value": value},
                )
            )
            assert not create.calls, (kind, value)
            assert "4.5" in json.dumps(out)


@pytest.mark.asyncio
async def test_valid_rir_config_still_goes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(WEIGHT_CFG)
    monkeypatch.setattr(routines.rir_config_create, "asyncio", create)
    await mcp.call_tool(
        "set_slot_entry_config", {"slot_entry_id": "2", "kind": "rir", "value": 2.5}
    )
    assert create.body.value == "2.5"


# ---------- config requirements ----------


@pytest.mark.asyncio
async def test_progression_is_unconditional_unless_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _Capture(WEIGHT_CFG)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", create)
    await mcp.call_tool(
        "set_slot_entry_config", {"slot_entry_id": "2", "kind": "weight", "value": 80}
    )
    assert create.body.requirements is UNSET


@pytest.mark.asyncio
async def test_progression_can_be_earned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The weight only goes up once the prescribed reps were actually logged."""
    mcp = _register()
    create = _Capture(WEIGHT_CFG)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", create)
    await mcp.call_tool(
        "set_slot_entry_config",
        {
            "slot_entry_id": "2",
            "kind": "weight",
            "value": 2.5,
            "iteration": 2,
            "operation": "+",
            "requirements": ["repetitions", "rir"],
        },
    )
    assert create.body.requirements == {"rules": ["repetitions", "rir"]}


@pytest.mark.asyncio
async def test_requirements_reach_every_config_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """wger puts the field on all ten config endpoints, not just weight."""
    mcp = _register()
    for kind, cfg in routines.CONFIG_KINDS.items():
        create = _Capture(WEIGHT_CFG)
        monkeypatch.setattr(cfg.create_mod, "asyncio", create)
        await mcp.call_tool(
            "set_slot_entry_config",
            {
                "slot_entry_id": "2",
                "kind": kind,
                "value": 1,
                "requirements": ["repetitions"],
            },
        )
        assert create.body.requirements == {"rules": ["repetitions"]}, kind


@pytest.mark.asyncio
async def test_unknown_requirement_rule_is_refused_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'reps' is this server's word for it; wger's rule is 'repetitions'."""
    mcp = _register()
    create = _Capture(WEIGHT_CFG)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", create)
    out = _result(
        await mcp.call_tool(
            "set_slot_entry_config",
            {
                "slot_entry_id": "2",
                "kind": "weight",
                "value": 80,
                "requirements": ["reps"],
            },
        )
    )
    assert not create.calls
    message = json.dumps(out)
    assert "reps" in message
    assert "repetitions" in message


@pytest.mark.asyncio
async def test_duplicate_rules_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _Capture(WEIGHT_CFG)
    monkeypatch.setattr(routines.weight_config_create, "asyncio", create)
    await mcp.call_tool(
        "set_slot_entry_config",
        {
            "slot_entry_id": "2",
            "kind": "weight",
            "value": 80,
            "requirements": ["rir", "repetitions", "rir"],
        },
    )
    assert create.body.requirements == {"rules": ["rir", "repetitions"]}


@pytest.mark.asyncio
async def test_empty_requirements_clear_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinct from omitting the argument, which leaves the gate as it was."""
    mcp = _register()
    patch = _Capture(WEIGHT_CFG)
    monkeypatch.setattr(routines.weight_config_partial_update, "asyncio", patch)
    await mcp.call_tool(
        "update_slot_entry_config",
        {"kind": "weight", "config_id": "3", "requirements": []},
    )
    assert patch.body.to_dict() == {"requirements": {"rules": []}}
