"""Profile tools: argument mapping onto the typed client."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from wger_api_client import models as api_models
from wger_api_client.types import UNSET

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import nutrition


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _register() -> MCPServer:
    mcp = MCPServer("test")
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )
    api = build_api_client(settings, _StubProvider())
    nutrition.register(mcp, api, settings)
    return mcp


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


PROFILE = api_models.Userprofile(
    username="alice",
    email="",
    email_verified=True,
    is_trustworthy=True,
    date_joined=datetime(2020, 1, 1, tzinfo=UTC),
    gym=None,
    is_temporary=False,
    last_workout_notification=None,
    height=180,
    age=40,
    gender="1",
)


@pytest.mark.asyncio
async def test_update_user_profile_passes_literal_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gender/intensity are Literal strings in the client and pass through."""
    mcp = _register()
    post = _Capture(PROFILE)
    monkeypatch.setattr(nutrition.userprofile_partial_update, "asyncio", post)
    await mcp.call_tool(
        "update_user_profile",
        {"gender": "2", "work_intensity": "3", "calories": 2000},
    )
    assert post.body.gender == "2"
    assert post.body.work_intensity == "3"
    assert post.body.calories == 2000
    assert post.body.sleep_hours is UNSET


@pytest.mark.asyncio
async def test_calculate_autofills_from_profile_and_weight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    monkeypatch.setattr(nutrition.userprofile_retrieve, "asyncio", _Capture(PROFILE))
    weights = SimpleNamespace(count=1, results=[SimpleNamespace(weight="82.5")])
    monkeypatch.setattr(nutrition.weightentry_list, "asyncio", _Capture(weights))
    result = await mcp.call_tool("calculate_daily_calories", {})
    data = result.structured_content
    assert data["inputs"]["sex"] == "male"
    assert data["inputs"]["height_cm"] == 180.0
    assert data["inputs"]["weight_kg"] == 82.5
    assert data["input_sources"]["sex"] == "userprofile"
