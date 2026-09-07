"""Workout log tools (per-set logging), via the generated ``wger_api_client``.

The legacy ``list_workouts`` tool is gone; its ``/workout/`` endpoint no
longer exists on wger >= 2.6.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
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
    profile_weight_unit,
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


def _uuid7_ms(log_id: Any) -> int | None:
    """Creation time in unix ms read out of a UUIDv7 row id, or None for any
    other id shape. wger's ``date`` field is day-granular, so the id is the
    only clock fine enough to see two writes seconds apart."""
    s = str(log_id).replace("-", "")
    if len(s) != 32 or s[12] != "7":
        return None
    try:
        return int(s[:12], 16)
    except ValueError:
        return None


async def _check_duplicate(
    api: AuthenticatedClient,
    settings: Settings,
    *,
    exercise_id: str,
    reps: float,
    weight: float,
    unit_id: int | None,
    confirm_duplicate: bool,
) -> None:
    """Refuse a row identical (exercise, weight, reps, unit) to one written
    moments ago: one physical set reported twice — typically a trainee message
    split in two, each half driving its own logging turn — not two sets. Off
    unless settings.duplicate_window_seconds is set; the window must sit below
    any real inter-set rest, so a genuine repeat minutes later passes."""
    window = settings.duplicate_window_seconds
    if not window or confirm_duplicate:
        return
    resp = await workoutlog_list.asyncio(
        client=api,
        limit=50,
        exercise=as_int(exercise_id, "exercise_id"),
        ordering="-date",
    )
    rows = [r.to_dict() for r in resp.results] if resp and resp.results else []
    cutoff_ms = datetime.now().timestamp() * 1000 - window * 1000
    for row in rows:
        ms = _uuid7_ms(row.get("id"))
        if ms is None or ms < cutoff_ms or row.get("weight_unit") != unit_id:
            continue
        try:
            same = Decimal(str(row.get("weight"))) == Decimal(str(weight)) and Decimal(
                str(row.get("repetitions"))
            ) == Decimal(str(reps))
        except InvalidOperation:
            continue
        if not same:
            continue
        age = int(datetime.now().timestamp() - ms / 1000)
        raise ToolInputError(
            f"an identical set ({weight:g} x {reps:g}) was already logged {age}s "
            f"ago as {row['id']}. If the trainee reported the SAME set again — "
            f"for example a report split across two messages — do not log it "
            f"twice: it is already recorded; answer from that row. Only for a "
            f"genuinely separate set completed within {window}s, re-send with "
            f"confirm_duplicate=true."
        )


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_tool
    async def log_set(
        exercise_id: str,
        reps: Annotated[float, Field(gt=0, le=REPS_MAX)],
        weight: Annotated[float, Field(ge=0, le=2000)],
        workout_log_date: date | datetime | None = None,
        rir: Annotated[float | None, Field(ge=0, le=RIR_MAX, multiple_of=RIR_STEP)] = None,
        weight_unit: str | None = None,
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
        confirm_duplicate: bool = False,
    ) -> dict[str, Any]:
        """Log a completed set (workoutlog). Without a date, wger stamps the
        entry with the current time; a bare date lands at 12:00.

        When the server is configured with a duplicate window
        (settings.duplicate_window_seconds, off by default), a row identical to
        one written seconds ago is refused, not written: that is one physical
        set reported twice — a trainee's message split in two — far more often
        than two real sets back to back. If it truly was a separate set, re-send
        with confirm_duplicate=true; if it was the same set, it is already
        logged, so answer from the existing row instead of logging.

        exercise_id is the movement performed, not always the one the plan
        names. For a substitution — machine occupied, equipment missing — pass
        the substitute's own id (search_exercises finds it) and still point
        routine_id, slot_entry_id and iteration at the planned slot: the set
        stays attached to the plan while the history stays true to what was
        lifted. Reusing the planned id files a rope pushdown as a machine
        pushdown, and nothing downstream can tell the two apart.

        routine_id, slot_entry_id and iteration attach the set to the plan it
        was performed from; get them from get_workout_for_date, whose planned
        entries carry the planned exercise_id alongside. Without them the set is
        logged as freestanding work: wger reads a routine's log view and its
        statistics through the routine link, so an unattached set is invisible
        there and in the apps that show a plan's progress.

        weight_unit is 'kg' or 'lb'; the weight is stored in the unit given,
        with no conversion. Omitted, it follows the trainee's wger profile, so a
        profile set to pounds does not silently record kilograms — and a profile
        that cannot be read refuses the log rather than guessing, so pass the
        unit to write anyway. weight itself has no default here, so an unloaded
        set is weight=0.

        reps_unit says what `reps` counts: repetitions (wger's default),
        until_failure, seconds, minutes, miles, kilometers, max_reps or meters.
        Pass the name, never a number — a number is refused here — and do not
        read an id off the order of that list. A plank logged without it is
        stored as 60 repetitions, not 60 seconds, which no later reading of the
        log can undo.

        rir records Reps In Reserve: how many good repetitions were left, wger's
        measure of set effort. A trainee reporting a range — "maybe 3 or 4" —
        gets the lower bound, the claim they are sure of; do not average it or
        pick a midpoint they never said. rest is the pause after, in seconds.

        The *_target fields record what was prescribed next to what was done, in
        the same row: reps_target, weight_target, rir_target, rest_target.
        get_workout_for_date supplies them, so passing them with the ids makes
        "did I hit the program" answerable from the log alone.

        This tool only inserts. To revise a set, call update_workout_log with
        the id from this call's result; calling log_set again writes a second
        row for one physical set, indistinguishable from two genuine sets at the
        same load.

        session_id attaches the set to a workout session (see
        list_workout_sessions); wger opens one for the day if none is given.
        next_log_id chains this set to the next log of a dropset series.
        """
        if slot_entry_id is not None and routine_id is None:
            raise ToolInputError(
                "slot_entry_id needs routine_id; both come from get_workout_for_date"
            )
        unit = as_weight_unit(
            weight_unit if weight_unit is not None else await profile_weight_unit(api)
        )
        await _check_duplicate(
            api,
            settings,
            exercise_id=exercise_id,
            reps=reps,
            weight=weight,
            unit_id=unit,
            confirm_duplicate=confirm_duplicate,
        )
        body = api_models.WorkoutLogRequest(
            exercise=as_int(exercise_id, "exercise_id"),
            repetitions=as_decimal(reps),
            repetitions_unit=opt(as_repetition_unit(reps_unit)),
            weight=as_decimal(weight),
            weight_unit=unit,
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
