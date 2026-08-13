"""Exercise search returns a lean shape: enough to pick an id, nothing more."""

from __future__ import annotations

import json
from typing import Any

import respx
from mcp.server.fastmcp import FastMCP

from wger_mcp.config import Settings
from wger_mcp.tools import exercises
from wger_mcp.wger_client import WgerClient

API = "https://wger.test/api/v2"


class _StubProvider:
    async def authorization_header(self) -> str:
        return "Token dev"

    async def aclose(self) -> None:
        pass


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_auth="none",
        wger_dev_token="dev",
    )


def _register() -> FastMCP:
    mcp = FastMCP("test")
    exercises.register(mcp, WgerClient(API, _StubProvider()), _settings())
    return mcp


def _mock_language(mock: Any) -> None:
    """Search resolves the configured code to wger's numeric language id."""
    mock.get("/language/").respond(
        json={"count": 1, "next": None, "results": [{"id": 2, "short_name": "en"}]}
    )


def _results(raw: Any) -> list[dict[str, Any]]:
    payload = raw[1] if isinstance(raw, tuple) else raw
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    return payload  # type: ignore[return-value]


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


async def test_search_omits_translations_images_and_uuid() -> None:
    mcp = _register()
    with respx.mock(base_url=API) as mock:
        _mock_language(mock)
        mock.get("/exerciseinfo/").respond(
            json={"count": 1, "next": None, "results": [_exercise()]}
        )
        out = _results(await mcp.call_tool("search_exercises", {"query": "bench press"}))

    assert len(out) == 1
    assert set(out[0]) == {"id", "name", "category", "equipment"}
    assert out[0]["id"] == 73
    assert out[0]["category"] == "Chest"
    assert out[0]["equipment"] == ["Barbell"]


async def test_search_payload_stays_small() -> None:
    """The point of the change: a search result must not carry a translation
    table into the model's context."""
    mcp = _register()
    with respx.mock(base_url=API) as mock:
        _mock_language(mock)
        mock.get("/exerciseinfo/").respond(
            json={"count": 1, "next": None, "results": [_exercise()]}
        )
        out = _results(await mcp.call_tool("search_exercises", {"query": "bench press"}))

    rendered = json.dumps(out)
    assert "lorem ipsum" not in rendered
    assert "uuid" not in rendered
    assert len(rendered) < 200


async def test_filter_search_keeps_muscles_but_drops_uuid() -> None:
    mcp = _register()
    with respx.mock(base_url=API) as mock:
        _mock_language(mock)
        mock.get("/exerciseinfo/").respond(
            json={"count": 1, "next": None, "results": [_exercise()]}
        )
        out = _results(await mcp.call_tool("search_exercises_by_filter", {"equipment_id": "1"}))

    assert set(out[0]) == {"id", "name", "category", "equipment", "muscles"}
    assert out[0]["muscles"] == ["Pectoralis major"]


async def test_get_exercise_still_returns_everything() -> None:
    """Detail is not lost, only moved: search picks the id, get_exercise expands it."""
    mcp = _register()
    with respx.mock(base_url=API) as mock:
        mock.get("/exerciseinfo/73/").respond(json=_exercise())
        out = _results(await mcp.call_tool("get_exercise", {"exercise_id": "73"}))

    assert out["uuid"]
    assert len(out["translations"]) == 16
    assert out["images"]


# ---------- batch search ----------


async def test_batch_resolves_many_names_in_one_call() -> None:
    """One call instead of one inference round trip per exercise."""
    mcp = _register()
    with respx.mock(base_url=API) as mock:
        _mock_language(mock)
        mock.get("/exerciseinfo/").respond(
            json={"count": 1, "next": None, "results": [_exercise()]}
        )
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


async def test_batch_collapses_duplicate_queries() -> None:
    mcp = _register()
    with respx.mock(base_url=API) as mock:
        _mock_language(mock)
        route = mock.get("/exerciseinfo/").respond(
            json={"count": 1, "next": None, "results": [_exercise()]}
        )
        out = _results(
            await mcp.call_tool(
                "search_exercises_batch", {"queries": ["bench press", "bench press"]}
            )
        )

    assert out["count"] == 1
    assert route.call_count == 1


async def test_batch_of_nothing_is_not_an_error() -> None:
    mcp = _register()
    out = _results(await mcp.call_tool("search_exercises_batch", {"queries": []}))
    assert out == {"count": 0, "results": {}}


async def test_name_comes_back_in_the_requested_language() -> None:
    """wger filters WHICH exercises match a language but returns every
    translation; picking the wrong one hands back a foreign name."""
    mcp = _register()
    exercise = _exercise()
    exercise["translations"] = [
        {"language": 1, "name": "Bankdrucken LH"},
        {"language": 2, "name": "Barbell Bench Press"},
        {"language": 13, "name": "Distensione su panca"},
    ]
    with respx.mock(base_url=API) as mock:
        _mock_language(mock)  # en -> 2
        mock.get("/exerciseinfo/").respond(
            json={"count": 1, "next": None, "results": [exercise]}
        )
        out = _results(await mcp.call_tool("search_exercises", {"query": "bench press"}))

    assert out[0]["name"] == "Barbell Bench Press"


async def test_specific_query_outranks_the_generic_match() -> None:
    """wger matches any word, so a plain "Bench Press" comes back for
    "incline barbell bench press". The specific one has to win."""
    mcp = _register()

    def variant(pk: int, name: str) -> dict[str, Any]:
        ex = _exercise()
        ex["id"] = pk
        ex["translations"] = [{"language": 2, "name": name}]
        return ex

    results = [
        variant(1, "Bench Press"),
        variant(2, "Bench Press Narrow Grip"),
        variant(3, "Incline Bench Press - Barbell"),
    ]
    with respx.mock(base_url=API) as mock:
        _mock_language(mock)
        mock.get("/exerciseinfo/").respond(
            json={"count": 3, "next": None, "results": results}
        )
        out = _results(
            await mcp.call_tool(
                "search_exercises",
                {"query": "incline barbell bench press", "limit": 2},
            )
        )

    assert [o["name"] for o in out] == ["Incline Bench Press - Barbell", "Bench Press"]
