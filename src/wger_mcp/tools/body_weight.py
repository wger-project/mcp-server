"""Body-weight tracking tools, via the generated ``wger_api_client``."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client import models as api_models
from wger_api_client.api.weightentry import (
    weightentry_create,
    weightentry_destroy,
    weightentry_list,
    weightentry_partial_update,
)
from wger_api_client.client import AuthenticatedClient

from ..api_client import paginate
from ..config import Settings
from .common import (
    MEASUREMENT_VALUE_MAX,
    api_list_tool,
    api_tool,
    as_decimal,
    as_uuid,
    at_noon,
    opt,
    require_fields,
)


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_tool
    async def log_body_weight(
        weight: Annotated[float, Field(ge=0, le=MEASUREMENT_VALUE_MAX)],
        when: date | datetime | None = None,
    ) -> dict[str, Any]:
        """Log a body-weight entry. Defaults to now; a bare date lands at 12:00.

        The value is read in the weight unit of the trainee's wger profile, kg
        for most people and lb for some — this endpoint takes no unit of its
        own, so 80 means 80 lb for an imperial profile. `whoami` reports which
        one it is, under `weight_unit`. Readings come back in that same unit.

        wger bounds a weight per unit (20-350 kg, 44-770 lb) and its refusal
        names the range, so an implausible value is answered rather than
        guessed at here.
        """
        # date is required here, so an omitted one becomes "now"
        body = api_models.WeightEntryRequest(
            date=at_noon(when) or datetime.now(UTC),
            weight=as_decimal(weight),
        )
        created = await weightentry_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_list_tool
    async def get_body_weight_history(
        limit: Annotated[int, Field(ge=1, le=500)] = 30,
    ) -> list[dict[str, Any]]:
        """Return recent body-weight entries (newest first), in the weight unit
        of the trainee's wger profile."""
        return await paginate(weightentry_list.asyncio, client=api, limit=limit, ordering="-date")

    @mcp.tool()
    @api_tool
    async def update_body_weight_entry(
        entry_id: str,
        weight: Annotated[float | None, Field(ge=0, le=MEASUREMENT_VALUE_MAX)] = None,
        when: date | datetime | None = None,
    ) -> dict[str, Any]:
        """Patch a body-weight entry. See log_body_weight for the unit `weight`
        is read in — a patch restamps the entry with the profile unit of the
        moment, so correcting a value after switching units rewrites what it
        means."""
        entry = as_uuid(entry_id, "entry_id")
        body = api_models.PatchedWeightEntryRequest(
            weight=opt(as_decimal(weight) if weight is not None else None),
            date=opt(at_noon(when)),
        )
        require_fields(body)
        updated = await weightentry_partial_update.asyncio(id=entry, client=api, body=body)
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_body_weight_entry(entry_id: str) -> dict[str, Any]:
        """Delete a body-weight entry."""
        await weightentry_destroy.asyncio_detailed(id=as_uuid(entry_id, "entry_id"), client=api)
        return {"deleted": True, "entry_id": entry_id}
