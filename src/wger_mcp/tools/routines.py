"""Routine / day / slot / slot-entry tools (the training-plan tree)."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..config import Settings
from ..wger_client import WgerClient, WgerError
from .common import WEIGHT_UNITS, bad_request, err

# Per-iteration config endpoints. Each config kind lives on its own
# resource linked by slot_entry; the entry itself only stores the
# exercise binding, not its sets/reps/weight.
SLOT_CONFIG_PATHS: dict[str, str] = {
    "sets": "sets-config/",
    "reps": "repetitions-config/",
    "weight": "weight-config/",
    "rir": "rir-config/",
    "rest": "rest-config/",
    "max_sets": "max-sets-config/",
    "max_reps": "max-repetitions-config/",
    "max_weight": "max-weight-config/",
    "max_rir": "max-rir-config/",
    "max_rest": "max-rest-config/",
}


def _unknown_kind(kind: str) -> dict[str, Any]:
    return bad_request(
        f"unknown kind '{kind}'; expected one of {sorted(SLOT_CONFIG_PATHS)}"
    )


def register(mcp: FastMCP, client: WgerClient, settings: Settings) -> None:
    @mcp.tool()
    async def list_routines(
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> list[dict[str, Any]]:
        """List the user's training routines (new wger model)."""
        try:
            return await client.paginate("routine/", limit=limit)
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
    async def get_routine(routine_id: str) -> dict[str, Any]:
        """Fetch a single routine with its day structure."""
        try:
            return await client.get(f"routine/{routine_id}/")
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def list_routine_days(
        routine_id: str,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """List training days of a routine."""
        try:
            return await client.paginate(
                "day/", params={"routine": routine_id, "ordering": "order"}, limit=limit
            )
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
    async def get_routine_day(day_id: str) -> dict[str, Any]:
        """Fetch a single training day."""
        try:
            return await client.get(f"day/{day_id}/")
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def list_slots(
        day_id: str,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """List slots in a training day."""
        try:
            return await client.paginate(
                "slot/", params={"day": day_id, "ordering": "order"}, limit=limit
            )
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
    async def list_slot_entries(
        slot_id: str,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """List exercise entries in a slot."""
        try:
            return await client.paginate(
                "slot-entry/", params={"slot": slot_id, "ordering": "order"}, limit=limit
            )
        except WgerError as exc:
            return [err(exc)]

    @mcp.tool()
    async def get_slot_entry(entry_id: str) -> dict[str, Any]:
        """Fetch a slot entry. Note: per-set sets/reps/weight/rir/rest are stored
        on separate *-config endpoints linked by slot_entry, not on the entry
        itself. Use list_slot_entry_configs to read them."""
        try:
            return await client.get(f"slot-entry/{entry_id}/")
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def list_slot_entry_configs(
        slot_entry_id: str,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch per-iteration configs for a slot entry. kinds filters which
        ones to read (e.g. ['sets','reps','weight']); default = all 10."""
        targets = kinds or list(SLOT_CONFIG_PATHS.keys())

        async def _fetch(kind: str) -> tuple[str, Any]:
            path = SLOT_CONFIG_PATHS.get(kind)
            if not path:
                return kind, {"error": True, "detail": f"unknown kind '{kind}'"}
            try:
                return kind, await client.paginate(
                    path,
                    params={"slot_entry": slot_entry_id, "ordering": "iteration"},
                    limit=200,
                )
            except WgerError as exc:
                return kind, err(exc)

        results = await asyncio.gather(*[_fetch(k) for k in targets])
        out: dict[str, Any] = {"slot_entry_id": slot_entry_id}
        for kind, value in results:
            out[kind] = value
        return out

    @mcp.tool()
    async def create_routine(
        name: Annotated[str, Field(min_length=1, max_length=255)],
        description: str = "",
        start: date | None = None,
        end: date | None = None,
        fit_in_week: bool = False,
    ) -> dict[str, Any]:
        """Create a training routine. Start defaults to today."""
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "start": (start or date.today()).isoformat(),
            "fit_in_week": fit_in_week,
        }
        if end is not None:
            payload["end"] = end.isoformat()
        try:
            return await client.post("routine/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def update_routine(
        routine_id: str,
        name: str | None = None,
        description: str | None = None,
        start: date | None = None,
        end: date | None = None,
        fit_in_week: bool | None = None,
    ) -> dict[str, Any]:
        """Patch a routine. Only provided fields are sent."""
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if start is not None:
            payload["start"] = start.isoformat()
        if end is not None:
            payload["end"] = end.isoformat()
        if fit_in_week is not None:
            payload["fit_in_week"] = fit_in_week
        if not payload:
            return bad_request("no fields to update")
        try:
            return await client.patch(f"routine/{routine_id}/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def add_routine_day(
        routine_id: str,
        name: Annotated[str, Field(min_length=1, max_length=255)],
        order: Annotated[int, Field(ge=1, le=100)],
        description: str = "",
        is_rest: bool = False,
        day_type: str = "standard",
    ) -> dict[str, Any]:
        """Add a training day to a routine."""
        payload: dict[str, Any] = {
            "routine": routine_id,
            "order": order,
            "name": name,
            "description": description,
            "is_rest": is_rest,
            "type": day_type,
        }
        try:
            return await client.post("day/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def add_slot_to_day(
        day_id: str,
        order: Annotated[int, Field(ge=1, le=100)],
        sets: Annotated[int | None, Field(ge=1, le=50)] = None,
        rest_seconds: Annotated[int | None, Field(ge=0, le=3600)] = None,
    ) -> dict[str, Any]:
        """Add an exercise slot (grouping) to a day."""
        payload: dict[str, Any] = {"day": day_id, "order": order}
        if sets is not None:
            payload["sets"] = sets
        if rest_seconds is not None:
            payload["rest"] = rest_seconds
        try:
            return await client.post("slot/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def update_routine_day(
        day_id: str,
        name: str | None = None,
        order: Annotated[int | None, Field(ge=1, le=100)] = None,
        description: str | None = None,
        is_rest: bool | None = None,
        day_type: str | None = None,
    ) -> dict[str, Any]:
        """Patch a training day. Only provided fields are sent."""
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if order is not None:
            payload["order"] = order
        if description is not None:
            payload["description"] = description
        if is_rest is not None:
            payload["is_rest"] = is_rest
        if day_type is not None:
            payload["type"] = day_type
        if not payload:
            return bad_request("no fields to update")
        try:
            return await client.patch(f"day/{day_id}/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def update_slot(
        slot_id: str,
        order: Annotated[int | None, Field(ge=1, le=100)] = None,
        sets: Annotated[int | None, Field(ge=1, le=50)] = None,
        rest_seconds: Annotated[int | None, Field(ge=0, le=3600)] = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Patch a slot."""
        payload: dict[str, Any] = {}
        if order is not None:
            payload["order"] = order
        if sets is not None:
            payload["sets"] = sets
        if rest_seconds is not None:
            payload["rest"] = rest_seconds
        if comment is not None:
            payload["comment"] = comment
        if not payload:
            return bad_request("no fields to update")
        try:
            return await client.patch(f"slot/{slot_id}/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def update_slot_entry(
        slot_entry_id: str,
        exercise_id: str | None = None,
        order: Annotated[int | None, Field(ge=1, le=100)] = None,
        comment: str | None = None,
        repetition_unit: int | None = None,
        weight_unit: int | None = None,
    ) -> dict[str, Any]:
        """Patch a slot entry (the exercise binding)."""
        payload: dict[str, Any] = {}
        if exercise_id is not None:
            payload["exercise"] = exercise_id
        if order is not None:
            payload["order"] = order
        if comment is not None:
            payload["comment"] = comment
        if repetition_unit is not None:
            payload["repetition_unit"] = repetition_unit
        if weight_unit is not None:
            payload["weight_unit"] = weight_unit
        if not payload:
            return bad_request("no fields to update")
        try:
            return await client.patch(f"slot-entry/{slot_entry_id}/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def update_slot_entry_config(
        kind: str,
        config_id: str,
        value: float | None = None,
        iteration: Annotated[int | None, Field(ge=1, le=1000)] = None,
        operation: str | None = None,
        step: str | None = None,
        repeat: bool | None = None,
    ) -> dict[str, Any]:
        """Patch an existing per-iteration config record.
        kind selects the endpoint (sets, reps, weight, rir, rest, max_*).
        Use this to bump weight when progressing."""
        path = SLOT_CONFIG_PATHS.get(kind)
        if not path:
            return _unknown_kind(kind)
        payload: dict[str, Any] = {}
        if value is not None:
            payload["value"] = value
        if iteration is not None:
            payload["iteration"] = iteration
        if operation is not None:
            payload["operation"] = operation
        if step is not None:
            payload["step"] = step
        if repeat is not None:
            payload["repeat"] = repeat
        if not payload:
            return bad_request("no fields to update")
        try:
            return await client.patch(f"{path}{config_id}/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def delete_slot_entry_config(kind: str, config_id: str) -> dict[str, Any]:
        """Delete a per-iteration config record."""
        path = SLOT_CONFIG_PATHS.get(kind)
        if not path:
            return _unknown_kind(kind)
        try:
            await client.delete(f"{path}{config_id}/")
            return {"deleted": True, "kind": kind, "config_id": config_id}
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def delete_routine(routine_id: str) -> dict[str, Any]:
        """Delete a routine and its entire day/slot/entry tree."""
        try:
            await client.delete(f"routine/{routine_id}/")
            return {"deleted": True, "routine_id": routine_id}
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def delete_routine_day(day_id: str) -> dict[str, Any]:
        """Delete a training day (cascades to its slots and entries)."""
        try:
            await client.delete(f"day/{day_id}/")
            return {"deleted": True, "day_id": day_id}
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def delete_slot(slot_id: str) -> dict[str, Any]:
        """Delete a slot (cascades to its entries and configs)."""
        try:
            await client.delete(f"slot/{slot_id}/")
            return {"deleted": True, "slot_id": slot_id}
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def delete_slot_entry(slot_entry_id: str) -> dict[str, Any]:
        """Delete a slot entry (the exercise binding) and its configs."""
        try:
            await client.delete(f"slot-entry/{slot_entry_id}/")
            return {"deleted": True, "slot_entry_id": slot_entry_id}
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def attach_exercise_to_slot(
        slot_id: str,
        exercise_id: str,
        order: Annotated[int, Field(ge=1, le=100)] = 1,
        repetition_unit: int | None = None,
        weight_unit: int | None = None,
        comment: str = "",
    ) -> dict[str, Any]:
        """Attach an exercise to a slot. exercise_id is the numeric wger PK
        (same id used in log_set / exerciseinfo). Per-set reps/weight live on
        sets-config / repetitions-config / weight-config records, not here."""
        payload: dict[str, Any] = {
            "slot": slot_id,
            "exercise": exercise_id,
            "order": order,
            "comment": comment,
        }
        if repetition_unit is not None:
            payload["repetition_unit"] = repetition_unit
        if weight_unit is not None:
            payload["weight_unit"] = weight_unit
        try:
            return await client.post("slot-entry/", json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def set_slot_entry_config(
        slot_entry_id: str,
        kind: str,
        value: float,
        iteration: Annotated[int, Field(ge=1, le=1000)] = 1,
        operation: str = "r",
        step: str = "abs",
        repeat: bool = False,
    ) -> dict[str, Any]:
        """Create a per-iteration config record for a slot entry.
        kind: one of sets, reps, weight, rir, rest, max_sets, max_reps,
        max_weight, max_rir, max_rest. operation 'r' = replace, 'a' = add,
        's' = subtract. step 'abs' or 'percent'."""
        path = SLOT_CONFIG_PATHS.get(kind)
        if not path:
            return _unknown_kind(kind)
        payload: dict[str, Any] = {
            "slot_entry": slot_entry_id,
            "iteration": iteration,
            "value": value,
            "operation": operation,
            "step": step,
            "repeat": repeat,
        }
        try:
            return await client.post(path, json=payload)
        except WgerError as exc:
            return err(exc)

    @mcp.tool()
    async def add_exercise_with_sets(
        day_id: str,
        exercise_id: str,
        sets: Annotated[int, Field(ge=1, le=50)],
        reps: Annotated[int, Field(ge=1, le=1000)],
        weight: Annotated[float | None, Field(ge=0, le=2000)] = None,
        slot_order: Annotated[int, Field(ge=1, le=100)] = 1,
        rest_seconds: Annotated[int | None, Field(ge=0, le=3600)] = None,
        weight_unit: str = "kg",
        rir: Annotated[float | None, Field(ge=0, le=10)] = None,
    ) -> dict[str, Any]:
        """High-level convenience: create slot + slot-entry + sets/reps configs
        in one call. Returns the created ids. Partial failures are reported in
        the response.

        weight is optional — omit it to prescribe an exercise without a load,
        which is the honest thing to do before the trainee's working weights are
        known. weight_unit is 'kg' or 'lb' and is recorded on the entry, so the
        number is stored in the unit it was given in rather than converted.

        rir sets a Reps-In-Reserve target for the set, wger's autoregulation
        field: 2 means "stop with two good reps left".
        """
        if weight_unit not in WEIGHT_UNITS:
            return bad_request(
                f"unknown weight_unit '{weight_unit}'; expected one of {', '.join(WEIGHT_UNITS)}"
            )
        result: dict[str, Any] = {}
        slot_payload: dict[str, Any] = {"day": day_id, "order": slot_order}
        if rest_seconds is not None:
            slot_payload["rest"] = rest_seconds
        try:
            slot = await client.post("slot/", json=slot_payload)
        except WgerError as exc:
            return err(exc) | {"stage": "slot"}
        result["slot"] = slot
        slot_id = slot.get("id") if isinstance(slot, dict) else None
        if not slot_id:
            return result | {"error": True, "stage": "slot", "detail": "missing slot id"}

        try:
            entry = await client.post(
                "slot-entry/",
                json={
                    "slot": slot_id,
                    "exercise": exercise_id,
                    "order": 1,
                    "comment": "",
                    "weight_unit": WEIGHT_UNITS[weight_unit],
                },
            )
        except WgerError as exc:
            return result | err(exc) | {"stage": "slot-entry"}
        result["slot_entry"] = entry
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if not entry_id:
            return result | {"error": True, "stage": "slot-entry", "detail": "missing entry id"}

        configs: list[tuple[str, float]] = [("sets", sets), ("reps", reps)]
        if weight is not None:
            configs.append(("weight", weight))
        if rir is not None:
            configs.append(("rir", rir))

        for kind, value in configs:
            try:
                result[f"{kind}_config"] = await client.post(
                    SLOT_CONFIG_PATHS[kind],
                    json={
                        "slot_entry": entry_id,
                        "iteration": 1,
                        "value": value,
                        "operation": "r",
                        "step": "abs",
                        "repeat": False,
                    },
                )
            except WgerError as exc:
                return result | err(exc) | {"stage": f"{kind}-config"}

        return result
