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
    api_list_tool,
    api_tool,
    as_decimal,
    as_int,
    at_noon,
    opt,
    opt_decimal,
    require_fields,
)


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_tool
    async def log_body_weight(
        weight_kg: Annotated[float, Field(gt=0, le=500)],
        when: date | datetime | None = None,
    ) -> dict[str, Any]:
        """Log a body-weight entry. Defaults to now; a bare date lands at 12:00."""
        # date is required here, so an omitted one becomes "now"
        body = api_models.WeightEntryRequest(
            date=at_noon(when) or datetime.now(UTC),
            weight=as_decimal(weight_kg),
        )
        created = await weightentry_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_list_tool
    async def get_body_weight_history(
        limit: Annotated[int, Field(ge=1, le=500)] = 30,
    ) -> list[dict[str, Any]]:
        """Return recent body-weight entries (newest first)."""
        return await paginate(weightentry_list.asyncio, client=api, limit=limit, ordering="-date")

    @mcp.tool()
    @api_tool
    async def update_body_weight_entry(
        entry_id: str,
        weight_kg: Annotated[float | None, Field(gt=0, le=500)] = None,
        when: date | datetime | None = None,
    ) -> dict[str, Any]:
        """Patch a body-weight entry."""
        entry = as_int(entry_id, "entry_id")
        body = api_models.PatchedWeightEntryRequest(
            weight=opt_decimal(weight_kg),
            date=opt(at_noon(when)),
        )
        require_fields(body)
        updated = await weightentry_partial_update.asyncio(id=entry, client=api, body=body)
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_body_weight_entry(entry_id: str) -> dict[str, Any]:
        """Delete a body-weight entry."""
        await weightentry_destroy.asyncio_detailed(id=as_int(entry_id, "entry_id"), client=api)
        return {"deleted": True, "entry_id": entry_id}
