"""Body-weight tracking tools, via the generated ``wger_api_client``.

Body weight is a measurement like any other since wger 2.7, kept in an official
``body_weight`` category that every user has from registration. These tools go
through ``/api/v2/measurement/`` rather than the ``/weightentry/`` shim, which
buys the one thing the shim structurally cannot do: **the unit travels with the
reading**. The shim reads and writes in whatever the profile says at that
moment, so a profile switched from kg to lb reinterprets everything written
before it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client import models as api_models
from wger_api_client.api.measurement import (
    measurement_create,
    measurement_destroy,
    measurement_list,
    measurement_partial_update,
    measurement_retrieve,
)
from wger_api_client.api.measurement_category import measurement_category_list
from wger_api_client.api.userprofile import userprofile_retrieve
from wger_api_client.client import AuthenticatedClient
from wger_api_client.types import UNSET

from ..api_client import paginate
from ..config import Settings
from .common import (
    MEASUREMENT_VALUE_MAX,
    ToolInputError,
    api_list_tool,
    api_tool,
    as_uuid,
    at_noon,
    opt,
    require_fields,
)

#: The only two units a body weight is kept in, which wger enforces as well
#: (``BODY_WEIGHT_UNITS`` in measurements/models/category.py).
WEIGHT_UNITS = ("kg", "lb")

#: wger's own factor (``AbstractWeight``), so a converted number here matches
#: what its web interface shows rather than being a second opinion.
KG_IN_LB = Decimal("2.20462262")

NOTES_MAX = 100


def as_weight_unit(unit: str) -> str:
    """Check a unit against the two a body weight can be kept in."""
    if unit not in WEIGHT_UNITS:
        raise ToolInputError(
            f"unknown weight unit '{unit}'; expected one of {', '.join(WEIGHT_UNITS)}"
        )
    return unit


def to_kg(value: float | None, unit: str | None) -> float | None:
    """The reading in kilograms, so a history spanning a unit change compares.

    Rounded to two places like wger's own ``Measurement.value_in``.
    """
    if value is None:
        return None
    if unit == "lb":
        return float(round(Decimal(str(value)) / KG_IN_LB, 2))
    return value


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    async def _category() -> dict[str, Any]:
        """The trainee's official body-weight category.

        Created for everyone at registration (core/signals.py), so a miss means
        something is wrong rather than that it has to be built here.
        """
        rows = await paginate(
            measurement_category_list.asyncio,
            client=api,
            limit=5,
            metric_type="body_weight",
            is_official=True,
        )
        if not rows:
            raise ToolInputError(
                "this account has no official body-weight category; wger creates one at "
                "registration, so this needs looking at on the server"
            )
        return rows[0]

    async def _profile_unit() -> str:
        """The weight unit the trainee thinks in."""
        profile = await userprofile_retrieve.asyncio(client=api)
        return (profile.to_dict() if profile else {}).get("weight_unit") or "kg"

    def _row(entry: dict[str, Any], category_unit: str | None) -> dict[str, Any]:
        """One entry as these tools report it: as recorded, plus normalised.

        The unit is per entry; an absent one means the category's, which is the
        same fallback wger applies.
        """
        unit = (entry.get("extra_data") or {}).get("unit") or category_unit
        value = entry.get("value")
        return {
            "id": entry.get("id"),
            "date": entry.get("date"),
            "weight": value,
            "unit": unit,
            "weight_kg": to_kg(value, unit),
            "notes": entry.get("notes") or None,
        }

    @mcp.tool()
    @api_tool
    async def log_body_weight(
        weight: Annotated[float, Field(ge=0, le=MEASUREMENT_VALUE_MAX)],
        when: date | datetime | None = None,
        unit: str | None = None,
        notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None,
    ) -> dict[str, Any]:
        """Log a body-weight entry. Defaults to now; a bare date lands at 12:00.

        unit is 'kg' or 'lb' and is recorded with the reading, so a history may
        hold both and nothing is reinterpreted later. Left out, it is the weight
        unit of the trainee's wger profile — passing it explicitly is both more
        precise and one request cheaper, since the profile then needs no lookup.

        wger bounds a weight per unit (20-350 kg, 44-770 lb) and its refusal
        names the range.
        """
        if unit is not None:
            as_weight_unit(unit)
            category, stored_unit = await _category(), unit
        else:
            # One round trip for both: neither depends on the other
            category, stored_unit = await asyncio.gather(_category(), _profile_unit())

        created = await measurement_create.asyncio(
            client=api,
            body=api_models.MeasurementRequest(
                category=as_uuid(category["id"], "category_id"),
                value=weight,
                date=at_noon(when) or datetime.now(UTC),
                notes=opt(notes),
                extra_data={"unit": stored_unit},
                source=UNSET,
            ),
        )
        return _row(created.to_dict(), category.get("unit"))

    @mcp.tool()
    @api_list_tool
    async def get_body_weight_history(
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 30,
    ) -> list[dict[str, Any]]:
        """Return body-weight entries (newest first), optionally within a date
        range. Both dates are inclusive.

        Each row carries the reading as it was recorded (`weight` + `unit`) and
        the same number in kilograms (`weight_kg`). Both, because a history can
        genuinely mix units — every entry carried over from before wger 2.7 has
        its own unit stamped on it — and comparing across that mix without the
        normalised figure means doing the arithmetic by hand.

        For a trend rather than the entries themselves, summarize_measurements
        over this category is far cheaper.
        """
        category = await _category()
        filters: dict[str, Any] = {
            "ordering": "-date",
            "category": as_uuid(category["id"], "category_id"),
        }
        if date_from is not None:
            filters["date_gte"] = datetime.combine(date_from, time.min)
        if date_to is not None:
            filters["date_lt"] = datetime.combine(date_to + timedelta(days=1), time.min)
        entries = await paginate(measurement_list.asyncio, client=api, limit=limit, **filters)
        return [_row(entry, category.get("unit")) for entry in entries]

    @mcp.tool()
    @api_tool
    async def update_body_weight_entry(
        entry_id: str,
        weight: Annotated[float | None, Field(ge=0, le=MEASUREMENT_VALUE_MAX)] = None,
        when: date | datetime | None = None,
        unit: str | None = None,
        notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None,
    ) -> dict[str, Any]:
        """Patch a body-weight entry.

        Correcting a value keeps the unit the entry was recorded in; pass unit
        only to restate it. Deliberately unlike the old behaviour, where an edit
        restamped the entry with whatever the profile happened to say and could
        therefore change what the number meant.
        """
        entry = as_uuid(entry_id, "entry_id")

        extra: dict[str, Any] | None = None
        if unit is not None:
            as_weight_unit(unit)
            # A PATCH replaces extra_data as a whole, so the rest of it has to be
            # read and sent back. For a synced entry that is the provenance the
            # importer wrote, which restating a unit must not quietly drop.
            current = await measurement_retrieve.asyncio(id=entry, client=api)
            stored = (current.to_dict() if current else {}).get("extra_data") or {}
            extra = {**stored, "unit": unit}

        body = api_models.PatchedMeasurementRequest(
            value=opt(weight),
            date=opt(at_noon(when)),
            notes=opt(notes),
            extra_data=opt(extra),
            source=UNSET,
        )
        require_fields(body)
        updated = await measurement_partial_update.asyncio(id=entry, client=api, body=body)
        return _row(updated.to_dict(), None)

    @mcp.tool()
    @api_tool
    async def delete_body_weight_entry(entry_id: str) -> dict[str, Any]:
        """Delete a body-weight entry."""
        await measurement_destroy.asyncio_detailed(id=as_uuid(entry_id, "entry_id"), client=api)
        return {"deleted": True, "entry_id": entry_id}
