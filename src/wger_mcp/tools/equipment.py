"""Equipment catalog tools, via the generated ``wger_api_client``.

The catalog is global and read-only in wger; the earlier create/update/delete
tools could never succeed and are gone.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field
from wger_api_client.api.equipment import equipment_list, equipment_retrieve
from wger_api_client.client import AuthenticatedClient

from ..api_client import paginate
from ..config import Settings
from .common import api_list_tool, api_tool, as_int


def register(mcp: MCPServer, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_list_tool
    async def list_gym_equipment(
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List the equipment catalog (barbell, dumbbell, ...)."""
        return await paginate(equipment_list.asyncio, client=api, limit=limit)

    @mcp.tool()
    @api_tool
    async def get_gym_equipment(equipment_id: str) -> dict[str, Any]:
        """Fetch a single equipment entry by ID."""
        item = await equipment_retrieve.asyncio(id=as_int(equipment_id, "equipment_id"), client=api)
        return item.to_dict()
