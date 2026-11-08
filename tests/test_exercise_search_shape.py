"""Exercise search returns a lean shape: enough to pick an id, nothing more."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import common, exercises


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
    exercises.register(mcp, build_api_client(settings, _StubProvider()), settings)
    return mcp


def _row(data: dict[str, Any]) -> Any:
    return SimpleNamespace(to_dict=lambda data=data: data)


class _Listing:
    """Stands in for a generated ``*_list.asyncio``; serves a fixed page."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(count=len(self.rows), results=[_row(r) for r in self.rows])


def _mock(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> _Listing:
    """Search resolves the configured code to wger's numeric language id first."""
    listing = _Listing(rows)
    monkeypatch.setattr(exercises.exerciseinfo_list, "asyncio", listing)
    monkeypatch.setattr(common.language_list, "asyncio", _Listing([{"id": 2, "short_name": "en"}]))
    return listing


def _results(raw: Any) -> Any:
    payload = raw.structured_content
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    return payload


# One hit, carrying the fields a real wger response carries. Sixteen translations
# is not unusual for a common lift.
def _exercise() -> dict[str, Any]:
    return {
        "id": 73,
        "uuid": "0a1b2c3d-0000-0000-0000-000000000000",
        "category": {"id": 11, "name": "Chest"},
        "equipment": [{"id": 1, "name": "Barbell"}],
        "muscles": [{"id": 4, "name": "Pectoralis major"}],
        "images": [{"image": "/media/x.png", "is_main": True, "thumbnails": {"small": "/s.png"}}],
        # language 2 is English in wger; the rest are noise the shaping must not pick
        "translations": [
            {"language": n, "name": f"Bench Press {n}", "description": "<p>lorem ipsum</p>"}
            for n in range(1, 17)
        ],
    }


@pytest.mark.asyncio
async def test_search_omits_translations_images_and_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    _mock(monkeypatch, [_exercise()])
    out = _results(await mcp.call_tool("search_exercises", {"query": "bench press"}))
    assert len(out) == 1
    assert set(out[0]) == {"id", "name", "category", "equipment"}
    assert out[0]["id"] == 73
    assert out[0]["category"] == "Chest"
    assert out[0]["equipment"] == ["Barbell"]


@pytest.mark.asyncio
async def test_search_payload_stays_small(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the change: a search result must not carry a translation
    table into the model's context."""
    mcp = _register()
    _mock(monkeypatch, [_exercise()])
    out = _results(await mcp.call_tool("search_exercises", {"query": "bench press"}))
    rendered = json.dumps(out)
    assert "lorem ipsum" not in rendered
    assert "uuid" not in rendered
    assert len(rendered) < 200


@pytest.mark.asyncio
async def test_filter_search_keeps_muscles_but_drops_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _register()
    _mock(monkeypatch, [_exercise()])
    out = _results(await mcp.call_tool("search_exercises_by_filter", {"equipment_id": "1"}))
    assert set(out[0]) == {"id", "name", "category", "equipment", "muscles"}
    assert out[0]["muscles"] == ["Pectoralis major"]


@pytest.mark.asyncio
async def test_get_exercise_still_returns_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detail is not lost, only moved: search picks the id, get_exercise expands it."""
    mcp = _register()

    async def retrieve(**kwargs: Any) -> Any:
        return _row(_exercise())

    monkeypatch.setattr(exercises.exerciseinfo_retrieve, "asyncio", retrieve)
    out = _results(await mcp.call_tool("get_exercise", {"exercise_id": "73"}))
    assert out["uuid"]
    assert len(out["translations"]) == 16
    assert out["images"]


# ---------- batch search ----------


@pytest.mark.asyncio
async def test_batch_resolves_many_names_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One call instead of one inference round trip per exercise."""
    mcp = _register()
    _mock(monkeypatch, [_exercise()])
    out = _results(
        await mcp.call_tool(
            "search_exercises_batch",
            {"queries": ["bench press", "cable fly", "lateral raise"]},
        )
    )
    assert out["count"] == 3
    assert set(out["results"]) == {"bench press", "cable fly", "lateral raise"}
    first = out["results"]["bench press"][0]
    assert set(first) == {"id", "name", "category", "equipment"}


@pytest.mark.asyncio
async def test_batch_collapses_duplicate_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    listing = _mock(monkeypatch, [_exercise()])
    out = _results(
        await mcp.call_tool("search_exercises_batch", {"queries": ["bench press", "bench press"]})
    )
    assert out["count"] == 1
    assert len(listing.calls) == 1


@pytest.mark.asyncio
async def test_batch_of_nothing_is_not_an_error() -> None:
    mcp = _register()
    out = _results(await mcp.call_tool("search_exercises_batch", {"queries": []}))
    assert out == {"count": 0, "results": {}}


@pytest.mark.asyncio
async def test_name_comes_back_in_the_requested_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wger filters WHICH exercises match a language but returns every
    translation; picking the wrong one hands back a foreign name."""
    mcp = _register()
    exercise = _exercise()
    exercise["translations"] = [
        {"language": 1, "name": "Bankdrucken LH"},
        {"language": 2, "name": "Barbell Bench Press"},
        {"language": 13, "name": "Distensione su panca"},
    ]
    _mock(monkeypatch, [exercise])
    out = _results(await mcp.call_tool("search_exercises", {"query": "bench press"}))
    assert out[0]["name"] == "Barbell Bench Press"


@pytest.mark.asyncio
async def test_specific_query_outranks_the_generic_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wger matches any word, so a plain "Bench Press" comes back for
    "incline barbell bench press". The specific one has to win."""
    mcp = _register()

    def variant(pk: int, name: str) -> dict[str, Any]:
        ex = _exercise()
        ex["id"] = pk
        ex["translations"] = [{"language": 2, "name": name}]
        return ex

    _mock(
        monkeypatch,
        [
            variant(1, "Bench Press"),
            variant(2, "Bench Press Narrow Grip"),
            variant(3, "Incline Bench Press - Barbell"),
        ],
    )
    out = _results(
        await mcp.call_tool(
            "search_exercises", {"query": "incline barbell bench press", "limit": 2}
        )
    )
    assert [o["name"] for o in out] == ["Incline Bench Press - Barbell", "Bench Press"]
