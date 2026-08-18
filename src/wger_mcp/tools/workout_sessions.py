"""Workout session tools, via the generated ``wger_api_client``.

A session is the training unit a day's sets belong to: when it ran, how it
felt, what the trainee wants to remember about it. wger opens one implicitly
for a log that names none, so the record exists either way — these tools are
what make its own fields reachable.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client import models as api_models
from wger_api_client.api.workoutsession import (
    workoutsession_create,
    workoutsession_destroy,
    workoutsession_list,
    workoutsession_partial_update,
    workoutsession_retrieve,
)
from wger_api_client.client import AuthenticatedClient

from ..api_client import paginate
from ..config import Settings
from .common import (
    ToolInputError,
    api_list_tool,
    api_tool,
    as_int,
    as_uuid,
    bad_request,
    opt,
    require_fields,
)

# wger stores the impression as a bare digit. An assistant handed a '1' has
# nothing to read it by and guesses; the trainee's own word for it does not.
IMPRESSIONS: dict[str, str] = {"bad": "1", "neutral": "2", "good": "3"}

# 'HH:MM' or 'HH:MM:SS', the same shape create_meal takes
_TIME_PATTERN = r"^\d{2}:\d{2}(:\d{2})?$"


def as_impression(impression: str | None) -> str | None:
    """Look up wger's code for 'bad', 'neutral' or 'good'."""
    if impression is None:
        return None
    try:
        return IMPRESSIONS[impression]
    except KeyError:
        raise ToolInputError(
            f"unknown impression '{impression}'; expected one of {', '.join(IMPRESSIONS)}"
        ) from None


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_list_tool
    async def list_workout_sessions(
        when: date | None = None,
        routine_id: str | None = None,
        impression: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> list[dict[str, Any]]:
        """List workout sessions, newest first.

        wger filters sessions on an exact date only, so `when` takes one day
        rather than a range; for a period, read the most recent `limit`
        sessions and go by their dates. impression filters on how the sessions
        felt: 'bad', 'neutral' or 'good'.
        """
        filters: dict[str, Any] = {"ordering": "-date"}
        if when is not None:
            filters["date"] = when
        if routine_id is not None:
            filters["routine"] = as_int(routine_id, "routine_id")
        if impression is not None:
            filters["impression"] = as_impression(impression)
        return await paginate(workoutsession_list.asyncio, client=api, limit=limit, **filters)

    @mcp.tool()
    @api_tool
    async def get_workout_session(session_id: str) -> dict[str, Any]:
        """Fetch one workout session."""
        session = await workoutsession_retrieve.asyncio(
            id=as_uuid(session_id, "session_id"), client=api
        )
        return session.to_dict()

    @mcp.tool()
    @api_tool
    async def log_workout_session(
        routine_id: str | None = None,
        day_id: str | None = None,
        when: date | None = None,
        notes: str | None = None,
        impression: str | None = None,
        time_start: Annotated[str | None, Field(pattern=_TIME_PATTERN)] = None,
        time_end: Annotated[str | None, Field(pattern=_TIME_PATTERN)] = None,
    ) -> dict[str, Any]:
        """Record a workout session: the date, how it went, and what to
        remember about it. Defaults to today.

        impression is 'bad', 'neutral' or 'good' — the trainee's own verdict on
        the session, which no aggregate over the logs can reconstruct. notes
        holds the rest of it (sleep, a tweaked shoulder, a gym that was full).

        time_start and time_end are 'HH:MM' or 'HH:MM:SS' and belong together:
        wger treats a session with only one of them as invalid, so pass both or
        neither.

        wger allows one session per routine per date. Logging a second one for
        the same day is refused; patch the existing session instead
        (list_workout_sessions with `when` finds it).
        """
        if (time_start is None) != (time_end is None):
            return bad_request("time_start and time_end must be given together")
        body = api_models.WorkoutSessionRequest(
            routine=opt(as_int(routine_id, "routine_id") if routine_id is not None else None),
            day=opt(as_int(day_id, "day_id") if day_id is not None else None),
            date=opt(when or date.today()),
            notes=opt(notes),
            impression=opt(as_impression(impression)),
            time_start=opt(time_start),
            time_end=opt(time_end),
        )
        created = await workoutsession_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def update_workout_session(
        session_id: str,
        routine_id: str | None = None,
        day_id: str | None = None,
        when: date | None = None,
        notes: str | None = None,
        impression: str | None = None,
        time_start: Annotated[str | None, Field(pattern=_TIME_PATTERN)] = None,
        time_end: Annotated[str | None, Field(pattern=_TIME_PATTERN)] = None,
    ) -> dict[str, Any]:
        """Patch a workout session. Only provided fields are sent.

        Unlike log_workout_session, one time may be sent on its own here: the
        session it lands on may already carry the other half. See
        log_workout_session for the fields themselves.
        """
        session = as_uuid(session_id, "session_id")
        body = api_models.PatchedWorkoutSessionRequest(
            routine=opt(as_int(routine_id, "routine_id") if routine_id is not None else None),
            day=opt(as_int(day_id, "day_id") if day_id is not None else None),
            date=opt(when),
            notes=opt(notes),
            impression=opt(as_impression(impression)),
            time_start=opt(time_start),
            time_end=opt(time_end),
        )
        require_fields(body)
        updated = await workoutsession_partial_update.asyncio(id=session, client=api, body=body)
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_workout_session(session_id: str) -> dict[str, Any]:
        """Delete a workout session. Its logged sets go with it."""
        await workoutsession_destroy.asyncio_detailed(
            id=as_uuid(session_id, "session_id"), client=api
        )
        return {"deleted": True, "session_id": session_id}
