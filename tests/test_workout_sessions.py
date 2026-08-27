"""Workout sessions — the endpoint the server had no tools for at all.

wger opens a session for any log that names none, so the record was always
there; what was missing was any way to read or write its own fields.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import workout_sessions

SESSION_ID = "018f6f30-0000-7000-8000-000000000009"
SESSION = api_models.WorkoutSession(id=UUID(SESSION_ID))


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
    workout_sessions.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


def _creator(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    create = _Capture(SESSION)
    monkeypatch.setattr(workout_sessions.workoutsession_create, "asyncio", create)
    return create


# ---------- creating ----------


@pytest.mark.asyncio
async def test_session_start_is_left_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """wger defaults datetime_start to now; sending our own clock would only
    disagree with it across a timezone."""
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool("log_workout_session", {})
    assert create.body.to_dict() == {}


@pytest.mark.asyncio
async def test_impression_is_recorded_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An assistant handed a bare '1' has nothing to read it by."""
    mcp = _register()
    create = _creator(monkeypatch)
    for name, code in workout_sessions.IMPRESSIONS.items():
        await mcp.call_tool("log_workout_session", {"impression": name})
        assert create.body.impression == code


@pytest.mark.asyncio
async def test_unknown_impression_is_refused_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    out = _result(await mcp.call_tool("log_workout_session", {"impression": "great"}))
    assert not create.calls
    message = json.dumps(out)
    assert "great" in message
    assert "good" in message  # the error names the valid options


@pytest.mark.asyncio
async def test_session_carries_notes_and_its_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool(
        "log_workout_session",
        {
            "routine_id": "1",
            "day_id": "5",
            "started_at": "2026-08-18T18:00:00",
            "ended_at": "2026-08-18T19:15:00",
            "notes": "Shoulder tweaked on the last set",
            "impression": "bad",
        },
    )
    body = create.body
    assert (body.routine, body.day) == (1, 5)
    assert body.datetime_start == datetime(2026, 8, 18, 18, 0)
    assert body.datetime_end == datetime(2026, 8, 18, 19, 15)
    assert body.notes == "Shoulder tweaked on the last set"


@pytest.mark.asyncio
async def test_an_open_session_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session without an end is one that is still running — 2.7 models that
    deliberately, where the old date+time triple treated it as invalid."""
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool("log_workout_session", {"started_at": "2026-08-18T18:00:00"})
    body = create.body
    assert body.datetime_start == datetime(2026, 8, 18, 18, 0)
    assert "datetime_end" not in body.to_dict()


@pytest.mark.asyncio
async def test_an_end_before_the_start_is_refused_up_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    out = _result(
        await mcp.call_tool(
            "log_workout_session",
            {"started_at": "2026-08-18T19:00:00", "ended_at": "2026-08-18T18:00:00"},
        )
    )
    assert not create.calls
    assert "before" in json.dumps(out)


@pytest.mark.asyncio
async def test_a_bare_date_lands_at_noon(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    create = _creator(monkeypatch)
    await mcp.call_tool("log_workout_session", {"started_at": "2026-08-18"})
    assert create.body.datetime_start == datetime.combine(date(2026, 8, 18), time(12, 0))


# ---------- reading ----------


@pytest.mark.asyncio
async def test_listing_is_newest_first_and_filters_by_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = _Capture(api_models.PaginatedWorkoutSessionList(count=0, results=[]))
    monkeypatch.setattr(workout_sessions.workoutsession_list, "asyncio", listing)
    mcp = _register()
    await mcp.call_tool(
        "list_workout_sessions",
        {
            "date_from": "2026-08-18",
            "date_to": "2026-08-18",
            "routine_id": "1",
            "impression": "good",
        },
    )
    call = listing.calls[-1]
    assert call["ordering"] == "-datetime_start"
    # the upper bound is midnight *after* the last day, or everything logged on
    # it would be dropped
    assert call["datetime_start_gte"] == datetime(2026, 8, 18, 0, 0)
    assert call["datetime_start_lt"] == datetime(2026, 8, 19, 0, 0)
    assert call["routine"] == 1
    assert call["impression"] == "3"


@pytest.mark.asyncio
async def test_retrieve_parses_the_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    get = _Capture(SESSION)
    monkeypatch.setattr(workout_sessions.workoutsession_retrieve, "asyncio", get)
    mcp = _register()
    await mcp.call_tool("get_workout_session", {"session_id": SESSION_ID})
    assert get.calls[-1]["id"] == UUID(SESSION_ID)


# ---------- patching ----------


@pytest.mark.asyncio
async def test_patch_sends_only_what_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = _Capture(SESSION)
    monkeypatch.setattr(workout_sessions.workoutsession_partial_update, "asyncio", patch)
    mcp = _register()
    await mcp.call_tool("update_workout_session", {"session_id": SESSION_ID, "impression": "good"})
    assert patch.body.to_dict() == {"impression": "3"}


@pytest.mark.asyncio
async def test_patch_closes_an_open_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending only the end is how a running session is finished; the start it
    is checked against is the one already stored."""
    patch = _Capture(SESSION)
    monkeypatch.setattr(workout_sessions.workoutsession_partial_update, "asyncio", patch)
    mcp = _register()
    await mcp.call_tool(
        "update_workout_session",
        {"session_id": SESSION_ID, "ended_at": "2026-08-18T19:30:00"},
    )
    assert patch.body.to_dict() == {"datetime_end": "2026-08-18T19:30:00"}


@pytest.mark.asyncio
async def test_empty_patch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = _Capture(SESSION)
    monkeypatch.setattr(workout_sessions.workoutsession_partial_update, "asyncio", patch)
    mcp = _register()
    out = _result(await mcp.call_tool("update_workout_session", {"session_id": SESSION_ID}))
    assert not patch.calls
    assert "no fields to update" in json.dumps(out)
