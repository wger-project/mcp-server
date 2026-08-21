"""Routine / day / slot / slot-entry tools (the training-plan tree), via the
generated ``wger_api_client``. Resource ids stay opaque strings at the tool
boundary (ADR 0002)."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Annotated, Any, NamedTuple

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client import models as api_models
from wger_api_client.api.day import (
    day_create,
    day_destroy,
    day_list,
    day_partial_update,
    day_retrieve,
)
from wger_api_client.api.exercise_translation import exercise_translation_list
from wger_api_client.api.max_repetitions_config import (
    max_repetitions_config_create,
    max_repetitions_config_destroy,
    max_repetitions_config_list,
    max_repetitions_config_partial_update,
)
from wger_api_client.api.max_rest_config import (
    max_rest_config_create,
    max_rest_config_destroy,
    max_rest_config_list,
    max_rest_config_partial_update,
)
from wger_api_client.api.max_rir_config import (
    max_rir_config_create,
    max_rir_config_destroy,
    max_rir_config_list,
    max_rir_config_partial_update,
)
from wger_api_client.api.max_sets_config import (
    max_sets_config_create,
    max_sets_config_destroy,
    max_sets_config_list,
    max_sets_config_partial_update,
)
from wger_api_client.api.max_weight_config import (
    max_weight_config_create,
    max_weight_config_destroy,
    max_weight_config_list,
    max_weight_config_partial_update,
)
from wger_api_client.api.repetitions_config import (
    repetitions_config_create,
    repetitions_config_destroy,
    repetitions_config_list,
    repetitions_config_partial_update,
)
from wger_api_client.api.rest_config import (
    rest_config_create,
    rest_config_destroy,
    rest_config_list,
    rest_config_partial_update,
)
from wger_api_client.api.rir_config import (
    rir_config_create,
    rir_config_destroy,
    rir_config_list,
    rir_config_partial_update,
)
from wger_api_client.api.routine import (
    routine_create,
    routine_date_sequence_gym_list,
    routine_destroy,
    routine_list,
    routine_partial_update,
    routine_retrieve,
)
from wger_api_client.api.sets_config import (
    sets_config_create,
    sets_config_destroy,
    sets_config_list,
    sets_config_partial_update,
)
from wger_api_client.api.slot import (
    slot_create,
    slot_destroy,
    slot_list,
    slot_partial_update,
)
from wger_api_client.api.slot_entry import (
    slot_entry_create,
    slot_entry_destroy,
    slot_entry_list,
    slot_entry_partial_update,
    slot_entry_retrieve,
)
from wger_api_client.api.weight_config import (
    weight_config_create,
    weight_config_destroy,
    weight_config_list,
    weight_config_partial_update,
)
from wger_api_client.client import AuthenticatedClient
from wger_api_client.errors import UnexpectedStatus
from wger_api_client.models.day_type_enum import DAY_TYPE_ENUM_VALUES
from wger_api_client.models.operation_enum import OPERATION_ENUM_VALUES
from wger_api_client.models.step_enum import STEP_ENUM_VALUES
from wger_api_client.types import UNSET, Unset

from ..api_client import paginate
from ..config import Settings
from .common import (
    ToolInputError,
    api_err,
    api_list_tool,
    api_tool,
    as_decimal,
    as_int,
    as_weight_unit,
    bad_request,
    language_id_resolver,
    opt,
    require_fields,
    weight_unit_name,
)


class _ConfigApi(NamedTuple):
    list_mod: Any
    create_mod: Any
    update_mod: Any
    destroy_mod: Any
    request: type
    patched: type
    int_value: bool


# Per-iteration config endpoints. Each kind lives on its own resource linked
# by slot_entry; the entry itself only stores the exercise binding.
CONFIG_KINDS: dict[str, _ConfigApi] = {
    "sets": _ConfigApi(
        sets_config_list,
        sets_config_create,
        sets_config_partial_update,
        sets_config_destroy,
        api_models.SetNrConfigRequest,
        api_models.PatchedSetNrConfigRequest,
        True,
    ),
    "reps": _ConfigApi(
        repetitions_config_list,
        repetitions_config_create,
        repetitions_config_partial_update,
        repetitions_config_destroy,
        api_models.RepetitionsConfigRequest,
        api_models.PatchedRepetitionsConfigRequest,
        False,
    ),
    "weight": _ConfigApi(
        weight_config_list,
        weight_config_create,
        weight_config_partial_update,
        weight_config_destroy,
        api_models.WeightConfigRequest,
        api_models.PatchedWeightConfigRequest,
        False,
    ),
    "rir": _ConfigApi(
        rir_config_list,
        rir_config_create,
        rir_config_partial_update,
        rir_config_destroy,
        api_models.RiRConfigRequest,
        api_models.PatchedRiRConfigRequest,
        False,
    ),
    "rest": _ConfigApi(
        rest_config_list,
        rest_config_create,
        rest_config_partial_update,
        rest_config_destroy,
        api_models.RestConfigRequest,
        api_models.PatchedRestConfigRequest,
        True,
    ),
    "max_sets": _ConfigApi(
        max_sets_config_list,
        max_sets_config_create,
        max_sets_config_partial_update,
        max_sets_config_destroy,
        api_models.MaxSetNrConfigRequest,
        api_models.PatchedMaxSetNrConfigRequest,
        True,
    ),
    "max_reps": _ConfigApi(
        max_repetitions_config_list,
        max_repetitions_config_create,
        max_repetitions_config_partial_update,
        max_repetitions_config_destroy,
        api_models.MaxRepetitionsConfigRequest,
        api_models.PatchedMaxRepetitionsConfigRequest,
        False,
    ),
    "max_weight": _ConfigApi(
        max_weight_config_list,
        max_weight_config_create,
        max_weight_config_partial_update,
        max_weight_config_destroy,
        api_models.MaxWeightConfigRequest,
        api_models.PatchedMaxWeightConfigRequest,
        False,
    ),
    "max_rir": _ConfigApi(
        max_rir_config_list,
        max_rir_config_create,
        max_rir_config_partial_update,
        max_rir_config_destroy,
        api_models.MaxRiRConfigRequest,
        api_models.PatchedMaxRiRConfigRequest,
        False,
    ),
    "max_rest": _ConfigApi(
        max_rest_config_list,
        max_rest_config_create,
        max_rest_config_partial_update,
        max_rest_config_destroy,
        api_models.MaxRestConfigRequest,
        api_models.PatchedMaxRestConfigRequest,
        True,
    ),
}

DAY_TYPES = tuple(sorted(DAY_TYPE_ENUM_VALUES))
OPERATIONS = tuple(sorted(OPERATION_ENUM_VALUES))
STEPS = tuple(sorted(STEP_ENUM_VALUES))

# wger requires an end date on every routine; twelve weeks is a conventional
# training block.
DEFAULT_ROUTINE_WEEKS = 12

# Model field limits, so the caller is told before the server refuses
ROUTINE_NAME_MAX = 25
DAY_NAME_MAX = 20


def _config_value(cfg: _ConfigApi, kind: str, value: float) -> int | str:
    """The wire value for a config kind: whole numbers for sets and rest
    seconds, decimal strings for the rest."""
    if not cfg.int_value:
        return as_decimal(value)
    if value != int(value):
        raise ToolInputError(f"{kind} must be a whole number, got {value}")
    return int(value)


def _unknown_kind(kind: str) -> dict[str, Any]:
    return bad_request(f"unknown kind '{kind}'; expected one of {sorted(CONFIG_KINDS)}")


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    _language_id_for = language_id_resolver(api)
    # Exercise names live on translations, not on the exercise itself, and the
    # plan endpoints carry ids only. Resolved here so "what am I doing today"
    # answers with names: the alternative is the caller fetching a full
    # exerciseinfo record per exercise, which costs it far more context than
    # the one short string added per set here. Cached for the process, since
    # exercise names are static content.
    _name_cache: dict[int, str] = {}

    async def _cache_name(exercise_id: int, language_id: int | None) -> None:
        try:
            rows = await paginate(
                exercise_translation_list.asyncio, client=api, limit=50, exercise=exercise_id
            )
        except (UnexpectedStatus, httpx.HTTPError):
            return  # a name that will not resolve must not fail the whole plan
        named = [r for r in rows if isinstance(r, dict) and r.get("name")]
        best = next((r["name"] for r in named if r.get("language") == language_id), None)
        # Any translation still beats handing back a bare id.
        if best is None and named:
            best = named[0]["name"]
        if best:
            _name_cache[exercise_id] = best

    async def _exercise_names(exercise_ids: set[int]) -> dict[int, str]:
        missing = exercise_ids - _name_cache.keys()
        if missing:
            language_id = await _language_id_for(settings.default_language)
            await asyncio.gather(*[_cache_name(i, language_id) for i in missing])
        return {i: _name_cache[i] for i in exercise_ids if i in _name_cache}

    @mcp.tool()
    @api_list_tool
    async def list_routines(
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> list[dict[str, Any]]:
        """List the user's training routines (new wger model)."""
        return await paginate(routine_list.asyncio, client=api, limit=limit)

    @mcp.tool()
    @api_tool
    async def get_routine(routine_id: str) -> dict[str, Any]:
        """Fetch a single routine with its day structure."""
        routine = await routine_retrieve.asyncio(id=as_int(routine_id, "routine_id"), client=api)
        return routine.to_dict()

    @mcp.tool()
    @api_tool
    async def get_workout_for_date(
        routine_id: str,
        workout_date: date | None = None,
    ) -> dict[str, Any]:
        """What the routine prescribes on a given date. Defaults to today.

        Returns the day's label, its iteration, and one entry per planned SET
        — an exercise prescribed for three sets appears three times, which is
        wger's gym-mode view: each entry is one set to perform and then log.
        Each carries the exercise name, its slot_entry_id, and the planned
        repetitions, weight and RiR. Feed routine_id, slot_entry_id and
        iteration straight into log_set so the logged set attaches to the plan.

        This is the one call that answers "what am I doing today" and "what is
        in this program". Walking days, slots, entries and their configs costs
        dozens of requests and returns far more than anyone needs.

        A rest day returns planned: [] with is_rest_day true.
        """
        target = workout_date or date.today()
        sequence = await routine_date_sequence_gym_list.asyncio(
            id=as_int(routine_id, "routine_id"), client=api
        )
        for entry in sequence or []:
            if entry.date != target:
                continue
            planned: list[dict[str, Any]] = [
                {
                    "slot_entry_id": cfg.slot_entry_id,
                    "exercise_id": cfg.exercise,
                    "sets": cfg.sets,
                    "repetitions": cfg.repetitions,
                    "weight": cfg.weight,
                    "weight_unit": weight_unit_name(cfg.weight_unit),
                    "rir": cfg.rir,
                    "rest": cfg.rest,
                    "text_repr": cfg.text_repr,
                }
                for slot in entry.slots
                for cfg in slot.sets
            ]
            names = await _exercise_names(
                {p["exercise_id"] for p in planned if p["exercise_id"] is not None}
            )
            for p in planned:
                p["exercise_name"] = names.get(p["exercise_id"])
            day = entry.day
            return {
                "routine_id": routine_id,
                "date": entry.date.isoformat(),
                "iteration": entry.iteration,
                "label": entry.label,
                "day_id": day.id,
                # A day need not be named, and Unset would not survive the
                # tool boundary as JSON.
                "day_name": None if isinstance(day.name, Unset) else day.name,
                "is_rest_day": (day.is_rest is True) or not planned,
                "planned": planned,
            }

        # Outside the routine's date range, or it schedules no day on that date.
        return {
            "routine_id": routine_id,
            "date": target.isoformat(),
            "iteration": None,
            "label": None,
            "day_id": None,
            "day_name": None,
            "is_rest_day": True,
            "planned": [],
            "note": "no scheduled day on this date - it may fall outside the routine's range",
        }

    @mcp.tool()
    @api_list_tool
    async def list_routine_days(
        routine_id: str,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """List training days of a routine."""
        return await paginate(
            day_list.asyncio,
            client=api,
            limit=limit,
            routine=as_int(routine_id, "routine_id"),
            ordering="order",
        )

    @mcp.tool()
    @api_tool
    async def get_routine_day(day_id: str) -> dict[str, Any]:
        """Fetch a single training day."""
        day = await day_retrieve.asyncio(id=as_int(day_id, "day_id"), client=api)
        return day.to_dict()

    @mcp.tool()
    @api_list_tool
    async def list_slots(
        day_id: str,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """List slots in a training day."""
        return await paginate(
            slot_list.asyncio,
            client=api,
            limit=limit,
            day=as_int(day_id, "day_id"),
            ordering="order",
        )

    @mcp.tool()
    @api_list_tool
    async def list_slot_entries(
        slot_id: str,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """List exercise entries in a slot."""
        return await paginate(
            slot_entry_list.asyncio,
            client=api,
            limit=limit,
            slot=as_int(slot_id, "slot_id"),
            ordering="order",
        )

    @mcp.tool()
    @api_tool
    async def get_slot_entry(entry_id: str) -> dict[str, Any]:
        """Fetch a slot entry. Note: per-set sets/reps/weight/rir/rest are stored
        on separate *-config endpoints linked by slot_entry, not on the entry
        itself. Use list_slot_entry_configs to read them."""
        entry = await slot_entry_retrieve.asyncio(id=as_int(entry_id, "entry_id"), client=api)
        return entry.to_dict()

    @mcp.tool()
    @api_tool
    async def list_slot_entry_configs(
        slot_entry_id: str,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch per-iteration configs for a slot entry. kinds filters which
        ones to read (e.g. ['sets','reps','weight']); default = all 10."""
        try:
            entry_id = as_int(slot_entry_id, "slot_entry_id")
        except ValueError as exc:
            return bad_request(str(exc))
        targets = kinds or list(CONFIG_KINDS)

        async def _fetch(kind: str) -> tuple[str, Any]:
            cfg = CONFIG_KINDS.get(kind)
            if cfg is None:
                return kind, bad_request(f"unknown kind '{kind}'")
            try:
                return kind, await paginate(
                    cfg.list_mod.asyncio,
                    client=api,
                    limit=200,
                    slot_entry=entry_id,
                    ordering="iteration",
                )
            except (UnexpectedStatus, httpx.HTTPError) as exc:
                return kind, api_err(exc)

        results = await asyncio.gather(*[_fetch(k) for k in targets])
        out: dict[str, Any] = {"slot_entry_id": slot_entry_id}
        for kind, value in results:
            out[kind] = value
        return out

    @mcp.tool()
    @api_tool
    async def create_routine(
        name: Annotated[str, Field(min_length=1, max_length=ROUTINE_NAME_MAX)],
        description: str = "",
        start: date | None = None,
        end: date | None = None,
        fit_in_week: bool = False,
    ) -> dict[str, Any]:
        """Create a training routine.

        Start defaults to today. wger requires an end date, so one is derived
        from the start when not given (12 weeks).
        """
        start_date = start or date.today()
        end_date = end or start_date + timedelta(weeks=DEFAULT_ROUTINE_WEEKS)
        if end_date <= start_date:
            return bad_request("end must be after start")
        body = api_models.RoutineRequest(
            start=start_date,
            end=end_date,
            name=name,
            description=description,
            fit_in_week=fit_in_week,
        )
        created = await routine_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def update_routine(
        routine_id: str,
        name: Annotated[str | None, Field(max_length=ROUTINE_NAME_MAX)] = None,
        description: str | None = None,
        start: date | None = None,
        end: date | None = None,
        fit_in_week: bool | None = None,
    ) -> dict[str, Any]:
        """Patch a routine. Only provided fields are sent."""
        body = api_models.PatchedRoutineRequest(
            name=opt(name),
            description=opt(description),
            start=opt(start),
            end=opt(end),
            fit_in_week=opt(fit_in_week),
        )
        require_fields(body)
        updated = await routine_partial_update.asyncio(
            id=as_int(routine_id, "routine_id"), client=api, body=body
        )
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def add_routine_day(
        routine_id: str,
        name: Annotated[str, Field(min_length=1, max_length=DAY_NAME_MAX)],
        order: Annotated[int, Field(ge=1, le=100)],
        description: str = "",
        is_rest: bool = False,
        day_type: str = "custom",
    ) -> dict[str, Any]:
        """Add a training day to a routine.

        day_type is one of: custom, enom, amrap, hiit, tabata, edt, rft, afap.
        Leave it alone for ordinary strength training.
        """
        if day_type not in DAY_TYPE_ENUM_VALUES:
            return bad_request(
                f"unknown day type '{day_type}'; expected one of {', '.join(DAY_TYPES)}"
            )
        body = api_models.DayRequest(
            routine=as_int(routine_id, "routine_id"),
            order=order,
            name=name,
            description=description,
            is_rest=is_rest,
            type_=day_type,
        )
        created = await day_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def add_slot_to_day(
        day_id: str,
        order: Annotated[int, Field(ge=1, le=100)],
        comment: str = "",
    ) -> dict[str, Any]:
        """Add an exercise slot (grouping) to a day. Sets/reps/weight live on
        the *-config records of its entries, not on the slot."""
        body = api_models.SlotRequest(day=as_int(day_id, "day_id"), order=order, comment=comment)
        created = await slot_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def update_routine_day(
        day_id: str,
        name: Annotated[str | None, Field(max_length=DAY_NAME_MAX)] = None,
        order: Annotated[int | None, Field(ge=1, le=100)] = None,
        description: str | None = None,
        is_rest: bool | None = None,
        day_type: str | None = None,
    ) -> dict[str, Any]:
        """Patch a training day. Only provided fields are sent."""
        if day_type is not None and day_type not in DAY_TYPE_ENUM_VALUES:
            return bad_request(
                f"unknown day type '{day_type}'; expected one of {', '.join(DAY_TYPES)}"
            )
        body = api_models.PatchedDayRequest(
            name=opt(name),
            order=opt(order),
            description=opt(description),
            is_rest=opt(is_rest),
            type_=opt(day_type),
        )
        require_fields(body)
        updated = await day_partial_update.asyncio(
            id=as_int(day_id, "day_id"), client=api, body=body
        )
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def update_slot(
        slot_id: str,
        order: Annotated[int | None, Field(ge=1, le=100)] = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Patch a slot."""
        body = api_models.PatchedSlotRequest(
            order=opt(order),
            comment=opt(comment),
        )
        require_fields(body)
        updated = await slot_partial_update.asyncio(
            id=as_int(slot_id, "slot_id"), client=api, body=body
        )
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def update_slot_entry(
        slot_entry_id: str,
        exercise_id: str | None = None,
        order: Annotated[int | None, Field(ge=1, le=100)] = None,
        comment: str | None = None,
        repetition_unit: int | None = None,
        weight_unit: int | None = None,
    ) -> dict[str, Any]:
        """Patch a slot entry (the exercise binding)."""
        body = api_models.PatchedSlotEntryRequest(
            exercise=(as_int(exercise_id, "exercise_id") if exercise_id is not None else UNSET),
            order=opt(order),
            comment=opt(comment),
            repetition_unit=opt(repetition_unit),
            weight_unit=opt(weight_unit),
        )
        require_fields(body)
        updated = await slot_entry_partial_update.asyncio(
            id=as_int(slot_entry_id, "slot_entry_id"), client=api, body=body
        )
        return updated.to_dict()

    @mcp.tool()
    @api_tool
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
        cfg = CONFIG_KINDS.get(kind)
        if cfg is None:
            return _unknown_kind(kind)
        if operation is not None and operation not in OPERATION_ENUM_VALUES:
            return bad_request(f"operation must be one of {OPERATIONS}")
        if step is not None and step not in STEP_ENUM_VALUES:
            return bad_request(f"step must be one of {STEPS}")
        body = cfg.patched(
            value=_config_value(cfg, kind, value) if value is not None else UNSET,
            iteration=opt(iteration),
            operation=opt(operation),
            step=opt(step),
            repeat=opt(repeat),
        )
        require_fields(body)
        updated = await cfg.update_mod.asyncio(
            id=as_int(config_id, "config_id"), client=api, body=body
        )
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_slot_entry_config(kind: str, config_id: str) -> dict[str, Any]:
        """Delete a per-iteration config record."""
        cfg = CONFIG_KINDS.get(kind)
        if cfg is None:
            return _unknown_kind(kind)
        await cfg.destroy_mod.asyncio_detailed(id=as_int(config_id, "config_id"), client=api)
        return {"deleted": True, "kind": kind, "config_id": config_id}

    @mcp.tool()
    @api_tool
    async def delete_routine(routine_id: str) -> dict[str, Any]:
        """Delete a routine and its entire day/slot/entry tree."""
        await routine_destroy.asyncio_detailed(id=as_int(routine_id, "routine_id"), client=api)
        return {"deleted": True, "routine_id": routine_id}

    @mcp.tool()
    @api_tool
    async def delete_routine_day(day_id: str) -> dict[str, Any]:
        """Delete a training day (cascades to its slots and entries)."""
        await day_destroy.asyncio_detailed(id=as_int(day_id, "day_id"), client=api)
        return {"deleted": True, "day_id": day_id}

    @mcp.tool()
    @api_tool
    async def delete_slot(slot_id: str) -> dict[str, Any]:
        """Delete a slot (cascades to its entries and configs)."""
        await slot_destroy.asyncio_detailed(id=as_int(slot_id, "slot_id"), client=api)
        return {"deleted": True, "slot_id": slot_id}

    @mcp.tool()
    @api_tool
    async def delete_slot_entry(slot_entry_id: str) -> dict[str, Any]:
        """Delete a slot entry (the exercise binding) and its configs."""
        await slot_entry_destroy.asyncio_detailed(
            id=as_int(slot_entry_id, "slot_entry_id"), client=api
        )
        return {"deleted": True, "slot_entry_id": slot_entry_id}

    @mcp.tool()
    @api_tool
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
        body = api_models.SlotEntryRequest(
            slot=as_int(slot_id, "slot_id"),
            exercise=as_int(exercise_id, "exercise_id"),
            order=order,
            comment=comment,
            repetition_unit=opt(repetition_unit),
            weight_unit=opt(weight_unit),
        )
        created = await slot_entry_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def set_slot_entry_config(
        slot_entry_id: str,
        kind: str,
        value: float,
        iteration: Annotated[int, Field(ge=1, le=1000)] = 1,
        operation: str = "r",
        step: str = "abs",
        repeat: bool = False,
        weight_unit: str | None = None,
    ) -> dict[str, Any]:
        """Create a per-iteration config record for a slot entry.
        kind: one of sets, reps, weight, rir, rest, max_sets, max_reps,
        max_weight, max_rir, max_rest. operation 'r' = replace, '+' = add,
        '-' = subtract. step 'abs', 'percent' or 'na'.

        For kind='weight', pass weight_unit ('kg' or 'lb') to record what the
        number means. wger stores the unit on the slot ENTRY rather than on the
        weight config, so this patches the entry for you. Otherwise a weight set
        here is silently read in whatever unit the entry already had.
        """
        cfg = CONFIG_KINDS.get(kind)
        if cfg is None:
            return _unknown_kind(kind)
        if operation not in OPERATION_ENUM_VALUES:
            return bad_request(f"operation must be one of {OPERATIONS}")
        if step not in STEP_ENUM_VALUES:
            return bad_request(f"step must be one of {STEPS}")
        entry = as_int(slot_entry_id, "slot_entry_id")
        if weight_unit is not None:
            if kind not in ("weight", "max_weight"):
                return bad_request(
                    f"weight_unit applies to kind 'weight' or 'max_weight', not '{kind}'"
                )
            try:
                await slot_entry_partial_update.asyncio(
                    id=entry,
                    client=api,
                    body=api_models.PatchedSlotEntryRequest(
                        weight_unit=as_weight_unit(weight_unit)
                    ),
                )
            except (UnexpectedStatus, httpx.HTTPError) as exc:
                return api_err(exc) | {"stage": "slot-entry weight_unit"}
        body = cfg.request(
            slot_entry=entry,
            iteration=iteration,
            value=_config_value(cfg, kind, value),
            operation=operation,
            step=step,
            repeat=repeat,
        )
        created = await cfg.create_mod.asyncio(client=api, body=body)
        return created.to_dict()

    async def _discard_slot(slot_id: int) -> bool:
        """Best-effort delete of a slot this module has just created."""
        try:
            await slot_destroy.asyncio_detailed(id=slot_id, client=api)
        except (UnexpectedStatus, httpx.HTTPError):
            return False
        return True

    @mcp.tool()
    @api_tool
    async def add_exercise_with_sets(
        day_id: str,
        exercise_id: str,
        sets: Annotated[int, Field(ge=1, le=50)],
        reps: Annotated[int, Field(ge=1, le=1000)],
        weight: Annotated[float | None, Field(ge=0, le=2000)] = None,
        slot_order: Annotated[int, Field(ge=1, le=100)] = 1,
        weight_unit: str = "kg",
        rir: Annotated[float | None, Field(ge=0, le=10)] = None,
    ) -> dict[str, Any]:
        """High-level convenience: create slot + slot-entry + sets/reps configs
        in one call. Returns the created ids. Partial failures are reported in
        the response; if the exercise cannot be attached, the empty slot is
        deleted again, because nothing renders it and it cannot be found later.

        weight is optional: omit it to prescribe an exercise without a load,
        which is the honest thing to do before the trainee's working weights are
        known. weight_unit is 'kg' or 'lb' and is recorded on the entry, so the
        number is stored in the unit it was given in rather than converted.

        rir sets a Reps-In-Reserve target for the set, wger's autoregulation
        field: 2 means "stop with two good reps left".
        """
        # Parsed up front: the slot must not be created if a later id is bad
        day = as_int(day_id, "day_id")
        exercise = as_int(exercise_id, "exercise_id")
        unit = as_weight_unit(weight_unit)
        planned: list[tuple[str, float]] = [("sets", sets), ("reps", reps)]
        if weight is not None:
            planned.append(("weight", weight))
        if rir is not None:
            planned.append(("rir", rir))
        values = [(kind, _config_value(CONFIG_KINDS[kind], kind, value)) for kind, value in planned]

        result: dict[str, Any] = {}
        try:
            slot = await slot_create.asyncio(
                client=api, body=api_models.SlotRequest(day=day, order=slot_order)
            )
        except (UnexpectedStatus, httpx.HTTPError) as exc:
            return api_err(exc) | {"stage": "slot"}
        result["slot"] = slot.to_dict()

        try:
            entry = await slot_entry_create.asyncio(
                client=api,
                body=api_models.SlotEntryRequest(
                    slot=slot.id, exercise=exercise, order=1, comment="", weight_unit=unit
                ),
            )
        except (UnexpectedStatus, httpx.HTTPError) as exc:
            # A slot holding no entry renders nowhere in the plan, so a caller
            # cannot see it to clean it up. Undo it rather than leave it behind.
            rolled_back = await _discard_slot(slot.id)
            if rolled_back:
                result.pop("slot")
            return result | api_err(exc) | {"stage": "slot-entry", "slot_rolled_back": rolled_back}
        result["slot_entry"] = entry.to_dict()

        # The configs only depend on the entry, so they go out together
        async def _config(kind: str, value: int | str) -> Any:
            cfg = CONFIG_KINDS[kind]
            return await cfg.create_mod.asyncio(
                client=api,
                body=cfg.request(
                    slot_entry=entry.id,
                    iteration=1,
                    value=value,
                    operation="r",
                    step="abs",
                    repeat=False,
                ),
            )

        created = await asyncio.gather(
            *[_config(kind, value) for kind, value in values], return_exceptions=True
        )
        failed = None
        for (kind, _), outcome in zip(values, created, strict=True):
            if isinstance(outcome, BaseException):
                failed = failed or (kind, outcome)
                continue
            result[f"{kind}_config"] = outcome.to_dict()
        if failed is not None:
            kind, exc = failed
            if not isinstance(exc, UnexpectedStatus | httpx.HTTPError):
                raise exc
            return result | api_err(exc) | {"stage": f"{kind}-config"}

        return result
