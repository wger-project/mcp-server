"""Analytics aggregation: log timestamps collapse onto calendar days."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from wger_mcp.api_client import build_api_client
from wger_mcp.config import Settings
from wger_mcp.tools import analytics


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
    analytics.register(mcp, api, settings)
    return mcp


@pytest.fixture(autouse=True)
def _clear_meta_cache() -> None:
    """The metadata cache lives for the process, so tests must not inherit it."""
    analytics._EX_META_CACHE.clear()


def _log(
    day: str,
    hour: int,
    reps: str = "8.00",
    weight: str = "50.00",
    exercise: int = 1,
) -> Any:
    entry = {
        "id": f"{day}-{hour}-{exercise}",
        "exercise": exercise,
        "date": f"{day}T{hour:02d}:00:00+00:00",
        "repetitions": reps,
        "weight": weight,
    }
    return SimpleNamespace(to_dict=lambda entry=entry: entry)


def _listing(entries: list[Any]):
    async def fn(**kwargs: Any) -> Any:
        return SimpleNamespace(count=len(entries), results=entries)

    return fn


def _result(raw: Any) -> Any:
    return raw.structured_content


@pytest.mark.asyncio
async def test_sessions_group_by_day_not_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """wger stamps each set with a full timestamp; sets of one workout still
    form a single session."""
    mcp = _register()
    day = (date.today() - timedelta(days=1)).isoformat()
    logs = [_log(day, 10), _log(day, 10), _log(day, 11)]
    monkeypatch.setattr(analytics.workoutlog_list, "asyncio", _listing(logs))
    out = _result(await mcp.call_tool("exercise_history", {"exercise_id": "1"}))
    assert len(out["sessions"]) == 1
    assert out["sessions"][0]["date"] == day
    assert out["sessions"][0]["sets"] == 3


@pytest.mark.asyncio
async def test_volume_trend_buckets_timestamped_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    day = date.today() - timedelta(days=1)
    logs = [_log(day.isoformat(), 10), _log(day.isoformat(), 18)]
    monkeypatch.setattr(analytics.workoutlog_list, "asyncio", _listing(logs))
    out = _result(await mcp.call_tool("volume_trend", {"days": 7, "bucket": "day"}))
    assert len(out["series"]) == 1
    assert out["series"][0]["bucket_start"] == day.isoformat()
    assert out["series"][0]["sets"] == 2


class _CaptureExercises:
    """Stands in for ``exerciseinfo_list.asyncio``; records the ids asked for."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    async def __call__(self, *, id_in: list[int], **kwargs: Any) -> Any:
        self.calls.append(list(id_in))
        results = [
            SimpleNamespace(
                to_dict=lambda eid=eid: {"id": eid, "translations": [{"name": f"E{eid}"}]}
            )
            for eid in id_in
        ]
        return SimpleNamespace(count=len(results), results=results)


@pytest.mark.asyncio
async def test_exercise_meta_is_looked_up_in_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grouping resolves every exercise with a single id__in query."""
    mcp = _register()
    day = (date.today() - timedelta(days=1)).isoformat()
    logs = [_log(day, 10, exercise=1), _log(day, 11, exercise=2), _log(day, 12, exercise=1)]
    monkeypatch.setattr(analytics.workoutlog_list, "asyncio", _listing(logs))
    listing = _CaptureExercises()
    monkeypatch.setattr(analytics.exerciseinfo_list, "asyncio", listing)
    out = _result(await mcp.call_tool("volume_trend", {"days": 7, "group_by": "exercise"}))
    assert [sorted(call) for call in listing.calls] == [[1, 2]]
    assert {row["group"]["label"] for row in out["series"]} == {"E1", "E2"}


@pytest.mark.asyncio
async def test_exercise_meta_is_split_into_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long histories are chunked so the query string stays manageable."""
    mcp = _register()
    day = (date.today() - timedelta(days=1)).isoformat()
    count = analytics._EX_META_BATCH + 5
    logs = [_log(day, 10, exercise=eid) for eid in range(1, count + 1)]
    monkeypatch.setattr(analytics.workoutlog_list, "asyncio", _listing(logs))
    listing = _CaptureExercises()
    monkeypatch.setattr(analytics.exerciseinfo_list, "asyncio", listing)
    await mcp.call_tool("volume_trend", {"days": 7, "group_by": "exercise"})
    assert sorted(len(call) for call in listing.calls) == [5, analytics._EX_META_BATCH]


@pytest.mark.asyncio
async def test_exercise_meta_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    day = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(analytics.workoutlog_list, "asyncio", _listing([_log(day, 10)]))
    listing = _CaptureExercises()
    monkeypatch.setattr(analytics.exerciseinfo_list, "asyncio", listing)
    await mcp.call_tool("volume_trend", {"days": 7, "group_by": "exercise"})
    await mcp.call_tool("volume_trend", {"days": 7, "group_by": "exercise"})
    assert listing.calls == [[1]]


@pytest.mark.asyncio
async def test_weekly_summary_counts_active_days(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _register()
    d1 = (date.today() - timedelta(days=1)).isoformat()
    d2 = (date.today() - timedelta(days=2)).isoformat()
    logs = [_log(d1, 9), _log(d1, 10), _log(d2, 9)]
    monkeypatch.setattr(analytics.workoutlog_list, "asyncio", _listing(logs))
    out = _result(await mcp.call_tool("weekly_summary", {"days": 7}))
    assert out["exercises"][0]["active_days"] == 2
    assert out["total_sets"] == 3
