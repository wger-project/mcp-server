"""Tests for MCP_TOOLS: group selection, defaults, and unknown-name handling."""

from __future__ import annotations

import os
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings, load_settings
from wger_mcp.tools import GROUP_ALIASES, SELECTABLE, TOOL_GROUPS, off, register_all


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "wger_base_url": "https://wger.test",
        "mcp_auth": "none",
        "wger_dev_token": "dev",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _register(mcp: FastMCP, settings: Settings) -> None:
    """off talks to Open Food Facts, so it gets its own client."""
    register_all(
        mcp,
        build_api_client(settings, _StubProvider()),
        off.build_http(),
        settings,
    )


async def _tool_names(**overrides: Any) -> set[str]:
    settings = _settings(**overrides)
    mcp = FastMCP("test")
    _register(mcp, settings)
    return {tool.name for tool in await mcp.list_tools()}


async def test_default_registers_every_group() -> None:
    """No MCP_TOOLS means the full surface, as before this setting existed."""
    names = await _tool_names()
    assert "log_set" in names  # workout_logs
    assert "log_ingredient" in names  # nutrition
    assert "search_exercises" in names  # exercises
    assert "whoami" in names  # profile
    assert len(names) > 50


async def test_subset_registers_only_the_named_groups() -> None:
    names = await _tool_names(mcp_tools=["nutrition", "workout_logs"])
    assert "log_ingredient" in names
    assert "log_set" in names
    # Groups that were not asked for stay off the surface.
    assert "search_exercises" not in names
    assert "whoami" not in names
    assert "list_routines" not in names


async def test_subset_is_smaller_than_the_default() -> None:
    assert len(await _tool_names(mcp_tools=["profile"])) < len(await _tool_names())


async def test_repeated_group_registers_once() -> None:
    """A duplicate in the env must not raise on a second registration."""
    assert await _tool_names(mcp_tools=["profile", "profile"]) == await _tool_names(
        mcp_tools=["profile"]
    )


async def test_order_is_stable_regardless_of_env_order() -> None:
    forwards = await _tool_names(mcp_tools=["nutrition", "profile"])
    backwards = await _tool_names(mcp_tools=["profile", "nutrition"])
    assert forwards == backwards


async def test_unknown_group_is_rejected() -> None:
    """A typo must fail loudly, not silently drop tools."""
    with pytest.raises(ValueError) as excinfo:
        await _tool_names(mcp_tools=["nutrition", "nutriton"])
    message = str(excinfo.value)
    assert "nutriton" in message
    # The error has to say what the valid names are.
    assert "nutrition" in message


def test_env_var_is_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP_TOOLS follows the same CSV convention as ALLOWED_HOSTS."""
    monkeypatch.setenv("MCP_AUTH", "none")
    monkeypatch.setenv("WGER_API_KEY", "dev")
    monkeypatch.setenv("MCP_TOOLS", " Nutrition , workout_logs ")
    try:
        assert load_settings().mcp_tools == ["nutrition", "workout_logs"]
    finally:
        os.environ.pop("MCP_TOOLS", None)


def test_every_group_name_is_a_valid_selection() -> None:
    """TOOL_GROUPS is the documented list, so each entry must work on its own."""
    for group in TOOL_GROUPS:
        settings = _settings(mcp_tools=[group])
        mcp = FastMCP("test")
        _register(mcp, settings)


# ---------- the routines split ----------


async def test_routines_alias_still_means_the_whole_tree() -> None:
    """The group was split so an agent that follows a plan need not carry the
    authoring schemas. Configs that predate the split must not notice."""
    both = await _tool_names(mcp_tools=["routines_read", "routines_write"])
    assert await _tool_names(mcp_tools=["routines"]) == both


async def test_reading_a_plan_costs_none_of_the_authoring_tools() -> None:
    read = await _tool_names(mcp_tools=["routines_read"])
    assert "get_workout_for_date" in read  # what a plan-following agent needs
    assert "list_slot_entry_configs" in read
    assert not {n for n in read if n.startswith(("create_", "add_", "update_", "delete_"))}


async def test_authoring_half_carries_the_writes() -> None:
    write = await _tool_names(mcp_tools=["routines_write"])
    assert {"create_routine", "add_exercise_with_sets", "set_slot_entry_config"} <= write
    assert "get_workout_for_date" not in write


async def test_the_two_halves_do_not_overlap() -> None:
    read = await _tool_names(mcp_tools=["routines_read"])
    write = await _tool_names(mcp_tools=["routines_write"])
    assert not read & write


async def test_alias_is_offered_when_a_name_is_wrong() -> None:
    """The error names what may be written, so an alias is not a trap."""
    assert "routines" in SELECTABLE
    assert set(GROUP_ALIASES) <= set(SELECTABLE)
    with pytest.raises(ValueError, match="routines"):
        _register(FastMCP("test"), _settings(mcp_tools=["routine"]))
