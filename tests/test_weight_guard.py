"""log_set / update_workout_log refuse a weight off the configured loading grid
until it is confirmed or the exercise has shown an off-grid value before, so a
garbled report ("49ers" -> 49) does not enter the history as a real load. The
guard is off unless settings.weight_grid names a step for the unit."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import workout_logs

LOG = api_models.WorkoutLog(exercise=73)


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _register(grid: dict[str, str] | None = None) -> FastMCP:
    mcp = FastMCP("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
        weight_grid={"lb": "2.5"} if grid is None else grid,
    )
    workout_logs.register(mcp, build_api_client(settings, _StubProvider()), settings)
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


def _rows(items: list[dict[str, Any]]) -> Any:
    async def fn(**kwargs: Any) -> Any:
        return SimpleNamespace(
            count=len(items),
            results=[SimpleNamespace(to_dict=lambda row=row: row) for row in items],
        )

    return fn


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    history: list[dict[str, Any]] | None = None,
    *,
    slot_rounding: str | None = None,
    stored: Any = None,
) -> _Capture:
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    monkeypatch.setattr(workout_logs.workoutlog_partial_update, "asyncio", _Capture(LOG))
    monkeypatch.setattr(workout_logs.workoutlog_list, "asyncio", _rows(history or []))
    if slot_rounding is not None:
        monkeypatch.setattr(
            workout_logs.slot_entry_retrieve,
            "asyncio",
            _Capture(SimpleNamespace(weight_rounding=slot_rounding)),
        )
    if stored is not None:
        monkeypatch.setattr(workout_logs.workoutlog_retrieve, "asyncio", _Capture(stored))
    return create


async def _log(mcp: FastMCP, **over: Any) -> Any:
    args = {"exercise_id": "73", "reps": 8, "weight": 49, "weight_unit": "lb"}
    args.update(over)
    return _result(await mcp.call_tool("log_set", args))


# ---------- log_set ----------


@pytest.mark.asyncio
async def test_off_grid_weight_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _wire(monkeypatch, history=[])
    out = await _log(mcp, weight=49)

    assert not create.called
    body = json.dumps(out)
    assert "confirm_weight" in body and "50" in body  # names nearest grid load


@pytest.mark.asyncio
async def test_guard_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No configured grid: the wger-accepted weight is written unchanged."""
    mcp = _register(grid={})
    create = _wire(monkeypatch, history=[])
    await _log(mcp, weight=49)

    assert create.called


@pytest.mark.asyncio
async def test_exercise_with_off_grid_history_is_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """One off-grid value on record marks a stack machine: new odd values pass."""
    mcp = _register()
    create = _wire(monkeypatch, history=[{"weight": "16.50", "weight_unit": 2}])
    await _log(mcp, weight=21.5)  # a different off-grid value, never logged before

    assert create.called


@pytest.mark.asyncio
async def test_off_grid_history_other_unit_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 16.5 kg row must not whitelist a 49 lb attempt."""
    mcp = _register()
    create = _wire(monkeypatch, history=[{"weight": "16.50", "weight_unit": 1}])
    await _log(mcp, weight=49)

    assert not create.called


@pytest.mark.asyncio
async def test_confirmed_off_grid_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _wire(monkeypatch, history=[])
    await _log(mcp, weight=21.5, confirm_weight=True)

    assert create.called


@pytest.mark.asyncio
async def test_slot_rounding_overrides_the_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot pinning 1.25 lb rounding admits 16.25, off the 2.5 default grid."""
    mcp = _register()
    create = _wire(monkeypatch, history=[], slot_rounding="1.25")
    await _log(mcp, weight=16.25, routine_id="7", slot_entry_id="501")

    assert create.called


@pytest.mark.asyncio
async def test_on_grid_weight_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _wire(monkeypatch, history=[])
    await _log(mcp, weight=55)

    assert create.called


@pytest.mark.asyncio
async def test_bodyweight_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _wire(monkeypatch, history=[])
    await _log(mcp, weight=0)

    assert create.called


@pytest.mark.asyncio
async def test_kg_weight_is_not_checked_when_only_lb_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register(grid={"lb": "2.5"})
    create = _wire(monkeypatch, history=[])
    await _log(mcp, weight=61.23, weight_unit="kg")

    assert create.called


# ---------- update_workout_log ----------


@pytest.mark.asyncio
async def test_update_off_grid_weight_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A garble corrected in through the patch tool is grid-checked too. Exercise
    and unit are read from the stored log when the caller omits them."""
    mcp = _register()
    update = _wire(
        monkeypatch,
        history=[],
        stored=SimpleNamespace(exercise=73, weight_unit=2),
    )
    args = {"log_id": "01a07d69-0000-7000-8000-000000000001", "weight": 49}
    out = _result(await mcp.call_tool("update_workout_log", args))

    assert not update.called  # workoutlog_partial_update was not reached
    assert "confirm_weight" in json.dumps(out)


@pytest.mark.asyncio
async def test_update_on_grid_weight_is_patched(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    _wire(monkeypatch, history=[], stored=SimpleNamespace(exercise=73, weight_unit=2))
    patch = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_partial_update, "asyncio", patch)
    args = {"log_id": "01a07d69-0000-7000-8000-000000000001", "weight": 55}
    await mcp.call_tool("update_workout_log", args)

    assert patch.called
