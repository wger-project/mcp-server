"""Body weight after wger 2.7 turned the endpoint into a shim.

`WeightEntry` is gone; `/api/v2/weightentry/` is now backed by body-weight
`Measurement` rows and keeps its `{id, date, weight}` shape. The one contract
change is the id, an integer before and a UUID since — which is exactly what
these tools pass around, so it is what needs covering.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp import FastMCP
from wger_api_client import models as api_models

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import body_weight

ENTRY_ID = "018f6f30-0000-7000-8000-00000000000a"
ENTRY = api_models.WeightEntry(
    id=UUID(ENTRY_ID),
    weight="80.00",
    user=1,
    date=datetime(2026, 8, 18, 12, tzinfo=UTC),
)


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


class _Capture:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result

    @property
    def body(self) -> Any:
        return self.calls[-1]["body"]


def _register() -> FastMCP:
    mcp = FastMCP("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )
    body_weight.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _result(raw: Any) -> Any:
    return raw[1] if isinstance(raw, tuple) else raw


# ---------- the id is a UUID now ----------


@pytest.mark.asyncio
async def test_patch_sends_the_id_as_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.weightentry_partial_update, "asyncio", patch)
    mcp = _register()
    await mcp.call_tool("update_body_weight_entry", {"entry_id": ENTRY_ID, "weight_kg": 79.5})
    assert patch.calls[-1]["id"] == UUID(ENTRY_ID)


@pytest.mark.asyncio
async def test_delete_sends_the_id_as_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    destroy = _Capture(None)
    monkeypatch.setattr(body_weight.weightentry_destroy, "asyncio_detailed", destroy)
    mcp = _register()
    await mcp.call_tool("delete_body_weight_entry", {"entry_id": ENTRY_ID})
    assert destroy.calls[-1]["id"] == UUID(ENTRY_ID)


@pytest.mark.asyncio
async def test_an_integer_id_is_refused_up_front(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id every pre-2.7 caller holds. Refused with a message that says what
    is expected, rather than sent and 404'd."""
    destroy = _Capture(None)
    monkeypatch.setattr(body_weight.weightentry_destroy, "asyncio_detailed", destroy)
    mcp = _register()
    out = _result(await mcp.call_tool("delete_body_weight_entry", {"entry_id": "42"}))
    assert not destroy.calls
    assert "entry_id" in json.dumps(out)


# ---------- ordinary writes ----------


@pytest.mark.asyncio
async def test_a_bare_date_lands_at_noon(monkeypatch: pytest.MonkeyPatch) -> None:
    create = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.weightentry_create, "asyncio", create)
    mcp = _register()
    await mcp.call_tool("log_body_weight", {"weight_kg": 80, "when": "2026-08-18"})
    assert create.body.date == datetime(2026, 8, 18, 12, 0)
    assert create.body.weight == "80"


@pytest.mark.asyncio
async def test_an_empty_patch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = _Capture(ENTRY)
    monkeypatch.setattr(body_weight.weightentry_partial_update, "asyncio", patch)
    mcp = _register()
    out = _result(await mcp.call_tool("update_body_weight_entry", {"entry_id": ENTRY_ID}))
    assert not patch.calls
    assert "no fields to update" in json.dumps(out)
