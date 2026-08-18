"""Body measurement tools (categories + entries), via the generated
``wger_api_client``."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
from wger_api_client.api.measurement_category import (
    measurement_category_create,
    measurement_category_destroy,
    measurement_category_list,
    measurement_category_partial_update,
    measurement_category_retrieve,
)
from wger_api_client.client import AuthenticatedClient

from ..api_client import paginate
from ..config import Settings
from .common import api_list_tool, api_tool, as_uuid, at_noon, opt, require_fields

# Model field limits, so the caller is told before the server refuses
CATEGORY_NAME_MAX = 100
CATEGORY_UNIT_MAX = 30
NOTES_MAX = 100
VALUE_MAX = 5000


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    # ── Categories ──────────────────────────────────────────────────────────

    @mcp.tool()
    @api_list_tool
    async def list_measurement_categories(
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List all body measurement categories (e.g. Waist, Chest, Bicep)."""
        return await paginate(measurement_category_list.asyncio, client=api, limit=limit)

    @mcp.tool()
    @api_tool
    async def create_measurement_category(
        name: Annotated[str, Field(min_length=1, max_length=CATEGORY_NAME_MAX)],
        unit: Annotated[str, Field(min_length=1, max_length=CATEGORY_UNIT_MAX)] = "cm",
    ) -> dict[str, Any]:
        """Create a body measurement category (e.g. name='Bicep', unit='cm')."""
        created = await measurement_category_create.asyncio(
            client=api, body=api_models.CategoryRequest(name=name, unit=unit)
        )
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def get_measurement_category(category_id: str) -> dict[str, Any]:
        """Fetch a single measurement category by ID."""
        category = await measurement_category_retrieve.asyncio(
            id=as_uuid(category_id, "category_id"), client=api
        )
        return category.to_dict()

    @mcp.tool()
    @api_tool
    async def update_measurement_category(
        category_id: str,
        name: Annotated[str | None, Field(max_length=CATEGORY_NAME_MAX)] = None,
        unit: Annotated[str | None, Field(max_length=CATEGORY_UNIT_MAX)] = None,
    ) -> dict[str, Any]:
        """Rename or change the unit of a measurement category."""
        category = as_uuid(category_id, "category_id")
        body = api_models.PatchedCategoryRequest(name=opt(name), unit=opt(unit))
        require_fields(body)
        updated = await measurement_category_partial_update.asyncio(
            id=category, client=api, body=body
        )
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_measurement_category(category_id: str) -> dict[str, Any]:
        """Delete a measurement category and all its entries."""
        await measurement_category_destroy.asyncio_detailed(
            id=as_uuid(category_id, "category_id"), client=api
        )
        return {"deleted": True, "category_id": category_id}

    # ── Entries ──────────────────────────────────────────────────────────────

    @mcp.tool()
    @api_list_tool
    async def list_measurements(
        category_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List body measurement entries (newest first), optionally filtered by
        category and date range. Both dates are inclusive.

        Without a range a growing history has to be pulled in full and trimmed
        by the caller, which spends context on entries nobody asked for."""
        filters: dict[str, Any] = {"ordering": "-date"}
        if category_id is not None:
            filters["category"] = as_uuid(category_id, "category_id")
        if date_from is not None:
            filters["date_gte"] = datetime.combine(date_from, time.min)
        if date_to is not None:
            filters["date_lt"] = datetime.combine(date_to + timedelta(days=1), time.min)
        return await paginate(measurement_list.asyncio, client=api, limit=limit, **filters)

    @mcp.tool()
    @api_tool
    async def log_measurement(
        category_id: str,
        value: Annotated[float, Field(gt=0, le=VALUE_MAX)],
        when: date | datetime | None = None,
        notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None,
    ) -> dict[str, Any]:
        """Add a body measurement entry to a category. Defaults to now; a bare
        date lands at 12:00."""
        body = api_models.MeasurementRequest(
            category=as_uuid(category_id, "category_id"),
            value=value,
            date=opt(at_noon(when)),
            notes=opt(notes),
        )
        created = await measurement_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def get_measurement(measurement_id: str) -> dict[str, Any]:
        """Fetch a single body measurement entry by ID."""
        entry = await measurement_retrieve.asyncio(
            id=as_uuid(measurement_id, "measurement_id"), client=api
        )
        return entry.to_dict()

    @mcp.tool()
    @api_tool
    async def update_measurement(
        measurement_id: str,
        value: Annotated[float | None, Field(gt=0, le=VALUE_MAX)] = None,
        when: date | datetime | None = None,
        notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None,
        category_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch a body measurement entry.

        category_id moves the entry to another category, for one filed under
        the wrong one — otherwise the only remedy is to delete it and log it
        again, which loses nothing but costs the original date.
        """
        entry = as_uuid(measurement_id, "measurement_id")
        body = api_models.PatchedMeasurementRequest(
            value=opt(value),
            date=opt(at_noon(when)),
            notes=opt(notes),
            category=opt(as_uuid(category_id, "category_id") if category_id is not None else None),
        )
        require_fields(body)
        updated = await measurement_partial_update.asyncio(id=entry, client=api, body=body)
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_measurement(measurement_id: str) -> dict[str, Any]:
        """Delete a body measurement entry."""
        await measurement_destroy.asyncio_detailed(
            id=as_uuid(measurement_id, "measurement_id"), client=api
        )
        return {"deleted": True, "measurement_id": measurement_id}
