"""log_set refuses a row identical (exercise, weight, reps, unit) to one
written within settings.duplicate_window_seconds: one physical set reported
twice — a message split in two — not two sets. Off unless the window is set;
confirm_duplicate=true writes anyway."""

from __future__ import annotations

import json
from datetime import datetime
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


def _register(window: int = 60) -> FastMCP:
    mcp = FastMCP("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
        duplicate_window_seconds=window,
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


def _uuid7(age_seconds: float) -> str:
    """A UUIDv7-shaped id whose embedded timestamp is `age_seconds` ago."""
    ms = int((datetime.now().timestamp() - age_seconds) * 1000)
    h = f"{ms:012x}"
    return f"{h[:8]}-{h[8:12]}-7000-8000-000000000000"


def _row(
    age_seconds: float,
    weight: str = "150.00",
    reps: str = "5.00",
    slot_entry: int | None = None,
) -> dict[str, Any]:
    return {
        "id": _uuid7(age_seconds),
        "weight": weight,
        "repetitions": reps,
        "weight_unit": 2,
        "slot_entry": slot_entry,
    }


def _wire(monkeypatch: pytest.MonkeyPatch, history: list[dict[str, Any]]) -> _Capture:
    create = _Capture(LOG)
    monkeypatch.setattr(workout_logs.workoutlog_create, "asyncio", create)
    monkeypatch.setattr(workout_logs.workoutlog_list, "asyncio", _rows(history))
    return create


async def _log(mcp: FastMCP, **over: Any) -> Any:
    args = {"exercise_id": "73", "reps": 5, "weight": 150, "weight_unit": "lb"}
    args.update(over)
    return _result(await mcp.call_tool("log_set", args))


@pytest.mark.asyncio
async def test_identical_row_in_window_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _wire(monkeypatch, [_row(age_seconds=10)])
    out = await _log(mcp)

    assert not create.called
    body = json.dumps(out)
    assert "confirm_duplicate" in body and "already logged" in body


@pytest.mark.asyncio
async def test_guard_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register(window=0)
    create = _wire(monkeypatch, [_row(age_seconds=10)])
    await _log(mcp)

    assert create.called


@pytest.mark.asyncio
async def test_old_identical_row_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine straight-set repeat after real rest is not a duplicate."""
    mcp = _register()
    create = _wire(monkeypatch, [_row(age_seconds=180)])
    await _log(mcp)

    assert create.called


@pytest.mark.asyncio
async def test_confirmed_duplicate_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _wire(monkeypatch, [_row(age_seconds=10)])
    await _log(mcp, confirm_duplicate=True)

    assert create.called


@pytest.mark.asyncio
async def test_different_load_in_window_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _wire(monkeypatch, [_row(age_seconds=10, weight="135.00")])
    await _log(mcp)

    assert create.called


@pytest.mark.asyncio
async def test_non_uuid7_id_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """An integer or v4 id carries no clock; the guard cannot judge it."""
    mcp = _register()
    create = _wire(monkeypatch, [{**_row(age_seconds=10), "id": 12345}])
    await _log(mcp)

    assert create.called


@pytest.mark.asyncio
async def test_other_side_of_a_per_side_movement_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Left and right of a per-side exercise carry identical weight+reps but
    different slots; the second side must not be refused as a duplicate."""
    mcp = _register()
    create = _wire(monkeypatch, [_row(age_seconds=10, slot_entry=106)])
    await _log(mcp, slot_entry_id="105", routine_id="6")

    assert create.called


@pytest.mark.asyncio
async def test_same_slot_in_window_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The split-message race re-sends the SAME set — same slot — so a matching
    slot within the window is still caught."""
    mcp = _register()
    create = _wire(monkeypatch, [_row(age_seconds=10, slot_entry=105)])
    out = await _log(mcp, slot_entry_id="105", routine_id="6")

    assert not create.called
    assert "already logged" in json.dumps(out)
