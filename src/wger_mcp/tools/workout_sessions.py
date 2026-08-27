"""Workout session tools, via the generated ``wger_api_client``.

A session is the training unit a day's sets belong to: when it ran, how it
felt, what the trainee wants to remember about it. wger opens one implicitly
for a log that names none, so the record exists either way — these tools are
what make its own fields reachable.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
    at_noon,
    bad_request,
    opt,
    require_fields,
)

# wger stores the impression as a bare digit. An assistant handed a '1' has
# nothing to read it by and guesses; the trainee's own word for it does not.
IMPRESSIONS: dict[str, str] = {"bad": "1", "neutral": "2", "good": "3"}


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
        date_from: date | None = None,
        date_to: date | None = None,
        routine_id: str | None = None,
        impression: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> list[dict[str, Any]]:
        """List workout sessions, newest first, optionally within a date range.
        Both dates are inclusive; pass the same day twice for a single day.

        The range is cut on the day a session *started*, which is the day it
        counts for: an overnight session belongs to the evening it began, not
        to the morning it ended. impression filters on how the sessions felt:
        'bad', 'neutral' or 'good'.
        """
        filters: dict[str, Any] = {"ordering": "-datetime_start"}
        if date_from is not None:
            filters["datetime_start_gte"] = datetime.combine(date_from, time.min)
        if date_to is not None:
            filters["datetime_start_lt"] = datetime.combine(date_to + timedelta(days=1), time.min)
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
        started_at: date | datetime | None = None,
        ended_at: date | datetime | None = None,
        notes: str | None = None,
        impression: str | None = None,
    ) -> dict[str, Any]:
        """Record a workout session: when it ran, how it went, and what to
        remember about it. Defaults to starting now.

        impression is 'bad', 'neutral' or 'good' — the trainee's own verdict on
        the session, which no aggregate over the logs can reconstruct. notes
        holds the rest of it (sleep, a tweaked shoulder, a gym that was full).

        started_at and ended_at are full timestamps, so a session may run past
        midnight; a bare date lands at 12:00. Leaving ended_at out keeps the
        session open, which is what an assistant should do while the workout is
        still going — patch it when the trainee is done. wger caps how long a
        session may last (5 hours by default) and refuses one that ends before
        it starts.
        """
        start, end = at_noon(started_at), at_noon(ended_at)
        if start is not None and end is not None and end < start:
            return bad_request("ended_at is before started_at")
        body = api_models.WorkoutSessionRequest(
            routine=opt(as_int(routine_id, "routine_id") if routine_id is not None else None),
            day=opt(as_int(day_id, "day_id") if day_id is not None else None),
            datetime_start=opt(start),
            datetime_end=opt(end),
            notes=opt(notes),
            impression=opt(as_impression(impression)),
        )
        created = await workoutsession_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def update_workout_session(
        session_id: str,
        routine_id: str | None = None,
        day_id: str | None = None,
        started_at: date | datetime | None = None,
        ended_at: date | datetime | None = None,
        notes: str | None = None,
        impression: str | None = None,
    ) -> dict[str, Any]:
        """Patch a workout session. Only provided fields are sent.

        Sending ended_at on its own is how an open session is closed; the start
        it is checked against is the one already stored. See
        log_workout_session for the fields themselves.
        """
        session = as_uuid(session_id, "session_id")
        start, end = at_noon(started_at), at_noon(ended_at)
        if start is not None and end is not None and end < start:
            return bad_request("ended_at is before started_at")
        body = api_models.PatchedWorkoutSessionRequest(
            routine=opt(as_int(routine_id, "routine_id") if routine_id is not None else None),
            day=opt(as_int(day_id, "day_id") if day_id is not None else None),
            datetime_start=opt(start),
            datetime_end=opt(end),
            notes=opt(notes),
            impression=opt(as_impression(impression)),
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
