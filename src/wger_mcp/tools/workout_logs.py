"""Workout log tools (per-set logging + legacy workouts)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..config import Settings
from ..wger_client import WgerClient, WgerError
from .common import WEIGHT_UNITS, bad_request, err


def register(mcp: FastMCP, client: WgerClient, settings: Settings) -> None:
    @mcp.tool()
    async def list_workouts(
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> list[dict[str, Any]]:
        """List legacy workout plans."""
        try:
            return await client.paginate("workout/", limit=limit)
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
    async def log_set(
        exercise_id: str,
        reps: Annotated[int, Field(ge=1, le=1000)],
        weight: Annotated[float, Field(ge=0, le=2000)],
        workout_log_date: date | None = None,
        rir: Annotated[float | None, Field(ge=0, le=10)] = None,
        weight_unit: str = "kg",
    ) -> dict[str, Any]:
        """Log a completed set (workoutlog). Uses today if no date given.

        weight_unit is 'kg' or 'lb'. The weight is stored in the unit given, so
        a trainee who works in pounds gets pounds back out — no conversion, and
        no rounding drift from converting twice.

        rir records Reps In Reserve for the set: how many good repetitions were
        left. It is how wger tracks set effort.
        """
        if weight_unit not in WEIGHT_UNITS:
            return bad_request(
                f"unknown weight_unit '{weight_unit}'; expected one of {', '.join(WEIGHT_UNITS)}"
            )
        payload: dict[str, Any] = {
            "exercise": exercise_id,
            "repetitions": reps,
            "weight": weight,
            "weight_unit": WEIGHT_UNITS[weight_unit],
            "date": (workout_log_date or date.today()).isoformat(),
        }
        if rir is not None:
            payload["rir"] = rir
        try:
            return await client.post("workoutlog/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def list_workout_logs(
        date_from: date | None = None,
        date_to: date | None = None,
        exercise_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> list[dict[str, Any]]:
        """List workout log entries (individual sets) with optional date/exercise filters."""
        params: dict[str, Any] = {"ordering": "-date"}
        if date_from is not None:
            params["date__gte"] = date_from.isoformat()
        if date_to is not None:
            params["date__lte"] = date_to.isoformat()
        if exercise_id is not None:
            params["exercise"] = exercise_id
        try:
            return await client.paginate("workoutlog/", params=params, limit=limit)
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
    async def get_workout_log(log_id: str) -> dict[str, Any]:
        """Fetch one workout log entry."""
        try:
            return await client.get(f"workoutlog/{log_id}/")
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def update_workout_log(
        log_id: str,
        reps: Annotated[int | None, Field(ge=1, le=1000)] = None,
        weight: Annotated[float | None, Field(ge=0, le=2000)] = None,
        rir: Annotated[float | None, Field(ge=0, le=10)] = None,
        when: date | None = None,
        weight_unit: str | None = None,
    ) -> dict[str, Any]:
        """Patch a workout log entry. Only provided fields are sent.

        weight_unit ('kg' or 'lb') is only sent when given, so correcting reps
        alone leaves the recorded unit untouched.
        """
        if weight_unit is not None and weight_unit not in WEIGHT_UNITS:
            return bad_request(
                f"unknown weight_unit '{weight_unit}'; expected one of {', '.join(WEIGHT_UNITS)}"
            )
        payload: dict[str, Any] = {}
        if reps is not None:
            payload["repetitions"] = reps
        if weight is not None:
            payload["weight"] = weight
        if weight_unit is not None:
            payload["weight_unit"] = WEIGHT_UNITS[weight_unit]
        if rir is not None:
            payload["rir"] = rir
        if when is not None:
            payload["date"] = when.isoformat()
        if not payload:
            return bad_request("no fields to update")
        try:
            return await client.patch(f"workoutlog/{log_id}/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def delete_workout_log(log_id: str) -> dict[str, Any]:
        """Delete a workout log entry."""
        try:
            await client.delete(f"workoutlog/{log_id}/")
            return {"deleted": True, "log_id": log_id}
        except WgerError as exc:
            return err(exc)
