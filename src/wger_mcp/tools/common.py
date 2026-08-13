"""Shared helpers for tool modules."""

from __future__ import annotations

from typing import Any

from ..wger_client import WgerError

# wger's weight-unit ids (/api/v2/setting-weightunit/). Used by logging and by
# routine authoring, which record the unit in different places: a workout log
# carries its own weight_unit, while a planned set takes it from its slot entry.
WEIGHT_UNITS: dict[str, int] = {"kg": 1, "lb": 2}


def err(exc: WgerError) -> dict[str, Any]:
    """Shape a WgerError as a tool-response dict."""
    return {"error": True, "status": exc.status, "detail": exc.body}


def bad_request(detail: str) -> dict[str, Any]:
    """Shape a 400-style validation error as a tool-response dict."""
    return {"error": True, "status": 400, "detail": detail}
