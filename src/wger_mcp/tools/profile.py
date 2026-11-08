"""User-profile tools, via the generated ``wger_api_client``."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from wger_api_client.api.userprofile import userprofile_retrieve
from wger_api_client.client import AuthenticatedClient

from ..config import Settings
from .common import api_tool


def register(mcp: MCPServer, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_tool
    async def whoami() -> dict[str, Any]:
        """Return the wger user profile of the authenticated caller."""
        profile = await userprofile_retrieve.asyncio(client=api)
        return profile.to_dict()
