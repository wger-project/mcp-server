"""Workout log tools (per-set logging), via the generated ``wger_api_client``.

The legacy ``list_workouts`` tool is gone; its ``/workout/`` endpoint no
longer exists on wger >= 2.6.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client import models as api_models
from wger_api_client.api.workoutlog import (
    workoutlog_create,
    workoutlog_destroy,
    workoutlog_list,
    workoutlog_partial_update,
    workoutlog_retrieve,
)
from wger_api_client.client import AuthenticatedClient

from ..api_client import paginate
from ..config import Settings
from .common import (
    RIR_MAX,
    RIR_STEP,
    ToolInputError,
    api_list_tool,
    api_tool,
    as_decimal,
    as_int,
    as_repetition_unit,
    as_uuid,
    as_weight_unit,
    at_noon,
    opt,
    require_fields,
)

# wger stores repetitions as decimal(6, 2), so this is the field's own ceiling
# rather than a guess. It has to clear plain rep counts and a distance in
# meters alike, now that the unit is selectable.
REPS_MAX = 9999
# A plausibility bound of this server's own, not wger's: rest is an unbounded
# PositiveIntegerField upstream. Two hours is well past any real set, and a
# four-digit typo is far more likely than a genuine longer pause.
REST_MAX = 7200


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_tool
    async def log_set(
        exercise_id: str,
        reps: Annotated[float, Field(gt=0, le=REPS_MAX)],
        weight: Annotated[float, Field(ge=0, le=2000)],
        workout_log_date: date | datetime | None = None,
        rir: Annotated[float | None, Field(ge=0, le=RIR_MAX, multiple_of=RIR_STEP)] = None,
        weight_unit: str = "kg",
        routine_id: str | None = None,
        slot_entry_id: str | None = None,
        iteration: Annotated[int | None, Field(ge=1, le=1000)] = None,
        reps_unit: str | None = None,
        rest: Annotated[int | None, Field(ge=0, le=REST_MAX)] = None,
        reps_target: Annotated[float | None, Field(ge=0, le=REPS_MAX)] = None,
        weight_target: Annotated[float | None, Field(ge=0, le=2000)] = None,
        rir_target: Annotated[float | None, Field(ge=0, le=RIR_MAX, multiple_of=RIR_STEP)] = None,
        rest_target: Annotated[int | None, Field(ge=0, le=REST_MAX)] = None,
        session_id: str | None = None,
        next_log_id: str | None = None,
    ) -> dict[str, Any]:
        """Log a completed set (workoutlog). Without a date, wger stamps the
        entry with the current time; a bare date lands at 12:00.

        weight_unit is 'kg' or 'lb'. The weight is stored in the unit given, so
        a trainee who works in pounds gets pounds back out, with no rounding
        drift from converting twice.

        reps_unit says what `reps` counts: repetitions (wger's default),
        seconds, minutes, meters, kilometers, miles, until_failure or max_reps.
        A plank logged without it is stored as 60 repetitions rather than 60
        seconds, which no later reading of the log can undo.

        rir records Reps In Reserve for the set: how many good repetitions were
        left. It is how wger tracks set effort. rest is the pause after the set,
        in seconds.

        routine_id, slot_entry_id and iteration attach the set to the plan it
        was performed from; get all three from get_workout_for_date. Without
        them the set is still logged and still counts towards the exercise's
        history, but it is freestanding work: wger reads a routine's log view
        and its statistics through the routine link, so an unattached set is
        invisible there and in the apps that show a plan's progress.

        The *_target fields record what was prescribed next to what was done, in
        the same row: reps_target, weight_target, rir_target, rest_target.
        get_workout_for_date supplies the prescribed numbers, so pass them along
        with the ids and "did I hit the program" is answerable from the log
        alone, without re-reading the plan as it stands later.

        session_id attaches the set to a workout session (see
        list_workout_sessions); wger opens one for the day if none is given.
        next_log_id chains this set to the next log of a dropset series.
        """
        if slot_entry_id is not None and routine_id is None:
            raise ToolInputError(
                "slot_entry_id needs routine_id; both come from get_workout_for_date"
            )
        body = api_models.WorkoutLogRequest(
            exercise=as_int(exercise_id, "exercise_id"),
            repetitions=as_decimal(reps),
            repetitions_unit=opt(as_repetition_unit(reps_unit)),
            weight=as_decimal(weight),
            weight_unit=as_weight_unit(weight_unit),
            date=opt(at_noon(workout_log_date)),
            rir=opt(as_decimal(rir) if rir is not None else None),
            routine=opt(as_int(routine_id, "routine_id") if routine_id is not None else None),
            slot_entry=opt(
                as_int(slot_entry_id, "slot_entry_id") if slot_entry_id is not None else None
            ),
            iteration=opt(iteration),
            rest=opt(rest),
            repetitions_target=opt(as_decimal(reps_target) if reps_target is not None else None),
            weight_target=opt(as_decimal(weight_target) if weight_target is not None else None),
            rir_target=opt(as_decimal(rir_target) if rir_target is not None else None),
            rest_target=opt(rest_target),
            session=opt(as_uuid(session_id, "session_id") if session_id is not None else None),
            next_log=opt(as_uuid(next_log_id, "next_log_id") if next_log_id is not None else None),
        )
        created = await workoutlog_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_list_tool
    async def list_workout_logs(
        date_from: date | None = None,
        date_to: date | None = None,
        exercise_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> list[dict[str, Any]]:
        """List workout log entries (individual sets) with optional date/exercise filters."""
        filters: dict[str, Any] = {"ordering": "-date"}
        if date_from is not None:
            filters["date_gte"] = datetime.combine(date_from, time.min)
        if date_to is not None:
            filters["date_lt"] = datetime.combine(date_to + timedelta(days=1), time.min)
        if exercise_id is not None:
            filters["exercise"] = as_int(exercise_id, "exercise_id")
        return await paginate(workoutlog_list.asyncio, client=api, limit=limit, **filters)

    @mcp.tool()
    @api_tool
    async def get_workout_log(log_id: str) -> dict[str, Any]:
        """Fetch one workout log entry."""
        log = await workoutlog_retrieve.asyncio(id=as_uuid(log_id, "log_id"), client=api)
        return log.to_dict()

    @mcp.tool()
    @api_tool
    async def update_workout_log(
        log_id: str,
        reps: Annotated[float | None, Field(gt=0, le=REPS_MAX)] = None,
        weight: Annotated[float | None, Field(ge=0, le=2000)] = None,
        rir: Annotated[float | None, Field(ge=0, le=RIR_MAX, multiple_of=RIR_STEP)] = None,
        when: date | datetime | None = None,
        weight_unit: str | None = None,
        exercise_id: str | None = None,
        reps_unit: str | None = None,
        rest: Annotated[int | None, Field(ge=0, le=REST_MAX)] = None,
        reps_target: Annotated[float | None, Field(ge=0, le=REPS_MAX)] = None,
        weight_target: Annotated[float | None, Field(ge=0, le=2000)] = None,
        rir_target: Annotated[float | None, Field(ge=0, le=RIR_MAX, multiple_of=RIR_STEP)] = None,
        rest_target: Annotated[int | None, Field(ge=0, le=REST_MAX)] = None,
        routine_id: str | None = None,
        slot_entry_id: str | None = None,
        iteration: Annotated[int | None, Field(ge=1, le=1000)] = None,
        session_id: str | None = None,
        next_log_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch a workout log entry. Only provided fields are sent.

        weight_unit ('kg' or 'lb') is only sent when given, so correcting reps
        alone leaves the recorded unit untouched. The same holds for reps_unit
        and for every *_target field; see log_set for what they mean.

        routine_id / slot_entry_id / iteration attach a set that was logged
        freestanding to the plan it actually came from — the repair for a
        session logged before anyone knew which routine it belonged to. Unlike
        log_set, slot_entry_id may be sent on its own here: the stored log may
        already name the routine.

        exercise_id fixes a set logged against the wrong exercise, which
        otherwise means deleting the entry and logging it again.
        """
        log = as_uuid(log_id, "log_id")
        body = api_models.PatchedWorkoutLogRequest(
            repetitions=opt(as_decimal(reps) if reps is not None else None),
            repetitions_unit=opt(as_repetition_unit(reps_unit)),
            weight=opt(as_decimal(weight) if weight is not None else None),
            weight_unit=opt(as_weight_unit(weight_unit)),
            rir=opt(as_decimal(rir) if rir is not None else None),
            date=opt(at_noon(when)),
            exercise=opt(as_int(exercise_id, "exercise_id") if exercise_id is not None else None),
            rest=opt(rest),
            repetitions_target=opt(as_decimal(reps_target) if reps_target is not None else None),
            weight_target=opt(as_decimal(weight_target) if weight_target is not None else None),
            rir_target=opt(as_decimal(rir_target) if rir_target is not None else None),
            rest_target=opt(rest_target),
            routine=opt(as_int(routine_id, "routine_id") if routine_id is not None else None),
            slot_entry=opt(
                as_int(slot_entry_id, "slot_entry_id") if slot_entry_id is not None else None
            ),
            iteration=opt(iteration),
            session=opt(as_uuid(session_id, "session_id") if session_id is not None else None),
            next_log=opt(as_uuid(next_log_id, "next_log_id") if next_log_id is not None else None),
        )
        require_fields(body)
        updated = await workoutlog_partial_update.asyncio(id=log, client=api, body=body)
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_workout_log(log_id: str) -> dict[str, Any]:
        """Delete a workout log entry."""
        await workoutlog_destroy.asyncio_detailed(id=as_uuid(log_id, "log_id"), client=api)
        return {"deleted": True, "log_id": log_id}
