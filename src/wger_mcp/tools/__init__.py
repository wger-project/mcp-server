"""MCP tool modules, grouped by domain.

Each wger-facing module exposes ``register(mcp, api, settings)`` on the typed
``wger_api_client``; ``off`` talks to Open Food Facts through its own httpx
client. ``server.build_app`` calls them all by default, or only the groups
named in ``MCP_TOOLS``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from wger_api_client.client import AuthenticatedClient

from ..config import Settings
from . import (
    analytics,
    body_weight,
    equipment,
    exercises,
    measurements,
    nutrition,
    off,
    profile,
    routines,
    workout_logs,
    workout_sessions,
)

# The client argument is Any because off takes the Open Food Facts httpx
# client where the others take the wger one.
Registrar = Callable[[FastMCP, Any, Settings], None]

# Keyed by group name, which is the module name. Iteration order is the
# registration order, so it stays stable no matter how MCP_TOOLS is written.
_REGISTRARS: dict[str, Registrar] = {
    "profile": profile.register,
    "routines": routines.register,
    "workout_logs": workout_logs.register,
    "workout_sessions": workout_sessions.register,
    "body_weight": body_weight.register,
    "measurements": measurements.register,
    "equipment": equipment.register,
    "nutrition": nutrition.register,
    "exercises": exercises.register,
    "analytics": analytics.register,
    "off": off.register,
}

#: Every selectable group name, in registration order.
TOOL_GROUPS: tuple[str, ...] = tuple(_REGISTRARS)


def register_all(
    mcp: FastMCP, api: AuthenticatedClient, off_http: httpx.AsyncClient, settings: Settings
) -> None:
    """Register tool modules on the given FastMCP instance.

    Registers every group unless ``settings.mcp_tools`` names a subset. An
    unknown group name is an error rather than a silent omission: a typo would
    otherwise remove tools the operator believes are present.
    """
    selected = set(settings.mcp_tools) or set(TOOL_GROUPS)
    unknown = sorted(selected - set(TOOL_GROUPS))
    if unknown:
        raise ValueError(
            f"MCP_TOOLS names unknown tool group(s): {', '.join(unknown)}. "
            f"Valid groups: {', '.join(TOOL_GROUPS)}"
        )
    for name in TOOL_GROUPS:
        if name in selected:
            _REGISTRARS[name](mcp, off_http if name == "off" else api, settings)


__all__ = ["TOOL_GROUPS", "register_all"]
