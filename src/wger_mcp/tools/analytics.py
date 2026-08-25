"""Analytics tools: weekly summary, exercise history, PRs, volume trend,
compare. Reads workout logs through the generated ``wger_api_client`` and
aggregates client-side."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, NamedTuple

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client.api.exerciseinfo import exerciseinfo_list
from wger_api_client.api.workoutlog import workoutlog_list
from wger_api_client.client import AuthenticatedClient
from wger_api_client.errors import UnexpectedStatus

from ..api_client import paginate
from ..config import Settings
from .common import api_tool, as_int, bad_request, opt


class _Metric(NamedTuple):
    """One metric, in the two spellings it has.

    ``name`` is what the ``metrics`` argument accepts; ``field`` is the key it
    carries in a bucket and in a result row (the two differ for volume alone,
    which reports its unit). ``decimals`` is ``None`` for the counts, which
    stay whole numbers rather than being rounded into floats.
    """

    name: str
    field: str
    decimals: int | None


#: Every metric the aggregating tools report, in the order rows list them.
#: The buckets, the projection and compare_periods' deltas all read this, so a
#: new metric is an entry here plus the line in :func:`_accumulate` that knows
#: how to compute it — not five edits spread over the module.
METRICS: tuple[_Metric, ...] = (
    _Metric("volume", "volume_kg", 2),
    _Metric("sets", "sets", None),
    _Metric("reps", "reps", None),
    _Metric("top_weight", "top_weight", 2),
    _Metric("est_1rm", "est_1rm", 2),
)

VOLUME_METRICS: tuple[str, ...] = tuple(m.name for m in METRICS)
GROUP_BY_OPTIONS: tuple[str, ...] = ("none", "exercise", "muscle", "category")

# Exercise metadata (name/category/muscles) is effectively static per wger
# deployment; cache it process-wide across tool invocations.
_EX_META_CACHE: dict[int, dict[str, Any]] = {}
# Ids per request, so that a long history does not build an unwieldy URL
_EX_META_BATCH = 100


def _epley(weight: float, reps: int) -> float:
    return weight * (1 + reps / 30) if reps > 0 else 0.0


def _bucket_start(d: date, bucket: str) -> str:
    if bucket == "day":
        return d.isoformat()
    if bucket == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if bucket == "month":
        return d.replace(day=1).isoformat()
    raise ValueError(f"unknown bucket {bucket}")


def _groups_for(
    ex_id: int, group_by: str, ex_cache: dict[int, dict[str, Any]]
) -> list[tuple[int, str] | None]:
    if group_by == "none":
        return [None]
    info = ex_cache.get(ex_id) or {}
    if group_by == "exercise":
        trs = info.get("translations") or []
        label = next(
            (t.get("name") for t in trs if isinstance(t, dict) and t.get("name")),
            f"Exercise {ex_id}",
        )
        return [(ex_id, label)]
    if group_by == "category":
        cat = info.get("category") or {}
        return [(cat.get("id") or 0, cat.get("name") or "Unknown")]
    if group_by == "muscle":
        muscles = info.get("muscles") or []
        if not muscles:
            return [(0, "Unknown")]
        return [(m.get("id") or 0, m.get("name") or "Unknown") for m in muscles]
    return [None]


async def _load_ex_meta(
    api: AuthenticatedClient, log_entries: list[dict[str, Any]], group_by: str
) -> dict[int, dict[str, Any]]:
    if group_by == "none":
        return {}
    ex_ids: set[int] = set()
    for entry in log_entries:
        eid = entry.get("exercise")
        if isinstance(eid, int):
            ex_ids.add(eid)
    missing = [eid for eid in ex_ids if eid not in _EX_META_CACHE]

    async def _fetch(ids: list[int]) -> list[dict[str, Any]]:
        try:
            return await paginate(exerciseinfo_list.asyncio, client=api, limit=len(ids), id_in=ids)
        except (UnexpectedStatus, httpx.HTTPError):
            return []

    batches = [missing[i : i + _EX_META_BATCH] for i in range(0, len(missing), _EX_META_BATCH)]
    # A failed lookup stays uncached, so a transient error is retried on the
    # next call instead of being remembered as "this exercise has no data"
    for metas in await asyncio.gather(*[_fetch(b) for b in batches]):
        for meta in metas:
            if isinstance(meta.get("id"), int):
                _EX_META_CACHE[meta["id"]] = meta
    return {eid: _EX_META_CACHE[eid] for eid in ex_ids if eid in _EX_META_CACHE}


def _new_metric_bucket() -> dict[str, float]:
    # int zeros for the counts, so a set count leaves as 3 rather than 3.0
    return {m.field: (0 if m.decimals is None else 0.0) for m in METRICS}


def _accumulate(bucket: dict[str, float], reps: int, weight: float) -> None:
    bucket["volume_kg"] += reps * weight
    bucket["sets"] += 1
    bucket["reps"] += reps
    if weight > bucket["top_weight"]:
        bucket["top_weight"] = weight
    est = _epley(weight, reps)
    if est > bucket["est_1rm"]:
        bucket["est_1rm"] = est


def _rounded(value: float, metric: _Metric) -> Any:
    """A metric's value as it is reported: counts whole, the rest rounded."""
    return value if metric.decimals is None else round(value, metric.decimals)


def _project(bucket: dict[str, float], selected: list[str]) -> dict[str, Any]:
    return {m.field: _rounded(bucket[m.field], m) for m in METRICS if m.name in selected}


def _delta(a: dict[str, float], b: dict[str, float], selected: list[str]) -> dict[str, Any]:
    return {m.field: _rounded(a[m.field] - b[m.field], m) for m in METRICS if m.name in selected}


def _delta_pct(a: dict[str, float], b: dict[str, float], selected: list[str]) -> dict[str, Any]:
    """Change relative to ``b``. ``None`` where b is zero: everything is an
    increase from nothing, and no percentage says how much."""
    out: dict[str, Any] = {}
    for m in METRICS:
        if m.name not in selected:
            continue
        base = b[m.field]
        out[m.field] = None if base == 0 else round(((a[m.field] - base) / base) * 100, 1)
    return out


def _select_metrics(metrics: list[str] | None) -> list[str]:
    valid = set(VOLUME_METRICS)
    selected = [m for m in (metrics or list(VOLUME_METRICS)) if m in valid]
    return selected or list(VOLUME_METRICS)


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _entry_reps(entry: dict[str, Any]) -> int:
    return int(_safe_float(entry.get("repetitions")))


def _entry_day(entry: dict[str, Any]) -> date | None:
    """The calendar day of a log entry; its ``date`` is a full timestamp."""
    raw = entry.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _since(days: int) -> datetime:
    return datetime.combine(date.today() - timedelta(days=days - 1), time.min)


async def _fetch_logs(
    api: AuthenticatedClient,
    *,
    limit: int,
    since: datetime | None = None,
    until: datetime | None = None,
    exercise: int | None = None,
) -> list[dict[str, Any]]:
    return await paginate(
        workoutlog_list.asyncio,
        client=api,
        limit=limit,
        ordering="date",
        date_gte=opt(since),
        date_lt=opt(until),
        exercise=opt(exercise),
    )


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    @mcp.tool()
    @api_tool
    async def weekly_summary(
        days: Annotated[int, Field(ge=1, le=90)] = 7,
    ) -> dict[str, Any]:
        """Aggregate workoutlog over the last N days: sets/reps/volume per exercise."""
        since = _since(days)
        logs = await _fetch_logs(api, limit=1000, since=since)

        # The metric bucket plus the one thing this tool counts that no other
        # does: the distinct days the exercise was trained on.
        per_exercise: dict[int, dict[str, Any]] = defaultdict(
            lambda: {**_new_metric_bucket(), "dates": set()}
        )
        for entry in logs:
            ex_id = entry.get("exercise")
            if ex_id is None:
                continue
            bucket = per_exercise[ex_id]
            _accumulate(bucket, _entry_reps(entry), _safe_float(entry.get("weight")))
            if d := _entry_day(entry):
                bucket["dates"].add(d)

        breakdown = [
            {
                "exercise_id": ex_id,
                **_project(v, ["volume", "sets", "reps"]),
                "active_days": len(v["dates"]),
            }
            for ex_id, v in sorted(
                per_exercise.items(), key=lambda kv: kv[1]["volume_kg"], reverse=True
            )
        ]
        return {
            "since": since.date().isoformat(),
            "until": date.today().isoformat(),
            "total_sets": sum(v["sets"] for v in per_exercise.values()),
            "total_volume_kg": round(sum(v["volume_kg"] for v in per_exercise.values()), 2),
            "exercises": breakdown,
        }

    @mcp.tool()
    @api_tool
    async def exercise_history(
        exercise_id: str,
        days: Annotated[int, Field(ge=1, le=730)] = 90,
        limit: Annotated[int, Field(ge=1, le=2000)] = 500,
    ) -> dict[str, Any]:
        """Return chronological workout-log entries for one exercise over the
        last N days. Includes per-session aggregates (one session per day)."""
        since = _since(days)
        logs = await _fetch_logs(
            api, limit=limit, since=since, exercise=as_int(exercise_id, "exercise_id")
        )
        sessions: dict[str, dict[str, Any]] = defaultdict(
            lambda: {**_new_metric_bucket(), "entries": []}
        )
        for entry in logs:
            weight = _safe_float(entry.get("weight"))
            reps = _entry_reps(entry)
            day = _entry_day(entry)
            b = sessions[day.isoformat() if day else ""]
            _accumulate(b, reps, weight)
            b["entries"].append(
                {
                    "id": entry.get("id"),
                    "reps": reps,
                    "weight": weight,
                    "rir": entry.get("rir"),
                }
            )
        return {
            "exercise_id": exercise_id,
            "since": since.date().isoformat(),
            "until": date.today().isoformat(),
            "total_sets": sum(s["sets"] for s in sessions.values()),
            "sessions": [
                {
                    "date": d,
                    **_project(s, ["volume", "sets", "reps", "top_weight"]),
                    "entries": s["entries"],
                }
                for d, s in sorted(sessions.items())
            ],
        }

    @mcp.tool()
    @api_tool
    async def personal_records(
        exercise_id: str | None = None,
        days: Annotated[int, Field(ge=1, le=3650)] = 730,
    ) -> dict[str, Any]:
        """Compute PRs from workout logs: max weight, max reps, best
        Epley-estimated 1RM. If exercise_id is omitted, returns one record
        block per exercise."""
        since = _since(days)
        exercise = as_int(exercise_id, "exercise_id") if exercise_id is not None else None
        logs = await _fetch_logs(api, limit=5000, since=since, exercise=exercise)

        per_ex: dict[int, dict[str, Any]] = {}
        for entry in logs:
            ex_id = entry.get("exercise")
            if ex_id is None:
                continue
            weight = _safe_float(entry.get("weight"))
            reps = _entry_reps(entry)
            est_1rm = _epley(weight, reps)
            day = _entry_day(entry)
            day_str = day.isoformat() if day else None
            rec = per_ex.setdefault(
                ex_id,
                {
                    "exercise_id": ex_id,
                    "max_weight": {"value": 0.0, "reps": 0, "date": None, "log_id": None},
                    "max_reps": {"value": 0, "weight": 0.0, "date": None, "log_id": None},
                    "best_est_1rm": {
                        "value": 0.0,
                        "weight": 0.0,
                        "reps": 0,
                        "date": None,
                        "log_id": None,
                    },
                },
            )
            if weight > rec["max_weight"]["value"]:
                rec["max_weight"] = {
                    "value": weight,
                    "reps": reps,
                    "date": day_str,
                    "log_id": entry.get("id"),
                }
            if reps > rec["max_reps"]["value"]:
                rec["max_reps"] = {
                    "value": reps,
                    "weight": weight,
                    "date": day_str,
                    "log_id": entry.get("id"),
                }
            if est_1rm > rec["best_est_1rm"]["value"]:
                rec["best_est_1rm"] = {
                    "value": round(est_1rm, 2),
                    "weight": weight,
                    "reps": reps,
                    "date": day_str,
                    "log_id": entry.get("id"),
                }

        return {
            "since": since.date().isoformat(),
            "until": date.today().isoformat(),
            "records": sorted(
                per_ex.values(),
                key=lambda r: r["best_est_1rm"]["value"],
                reverse=True,
            ),
        }

    @mcp.tool()
    @api_tool
    async def volume_trend(
        days: Annotated[int, Field(ge=1, le=730)] = 60,
        bucket: str = "week",
        metrics: list[str] | None = None,
        group_by: str = "none",
        exercise_id: str | None = None,
    ) -> dict[str, Any]:
        """Time-bucketed training volume. bucket=day|week|month. group_by=
        none|exercise|muscle|category. For muscle, exercise volume is
        attributed to each primary muscle (sum per-muscle > global)."""
        if bucket not in ("day", "week", "month"):
            return bad_request("bucket must be day|week|month")
        if group_by not in GROUP_BY_OPTIONS:
            return bad_request(f"group_by must be one of {list(GROUP_BY_OPTIONS)}")
        selected = _select_metrics(metrics)
        since = _since(days)
        exercise = as_int(exercise_id, "exercise_id") if exercise_id is not None else None
        logs = await _fetch_logs(api, limit=5000, since=since, exercise=exercise)

        ex_cache = await _load_ex_meta(api, logs, group_by)
        buckets: dict[tuple, dict[str, float]] = defaultdict(_new_metric_bucket)
        for entry in logs:
            ex_id = entry.get("exercise")
            day = _entry_day(entry)
            if ex_id is None or day is None:
                continue
            weight = _safe_float(entry.get("weight"))
            reps = _entry_reps(entry)
            bkt = _bucket_start(day, bucket)
            for group in _groups_for(ex_id, group_by, ex_cache):
                key = (bkt, group)
                _accumulate(buckets[key], reps, weight)

        series: list[dict[str, Any]] = []
        for (bkt, group), m in sorted(
            buckets.items(), key=lambda kv: (kv[0][0], -kv[1]["volume_kg"])
        ):
            row: dict[str, Any] = {"bucket_start": bkt}
            if group_by != "none" and group is not None:
                row["group"] = {"key": group[0], "label": group[1]}
            row.update(_project(m, selected))
            series.append(row)

        return {
            "since": since.date().isoformat(),
            "until": date.today().isoformat(),
            "bucket": bucket,
            "group_by": group_by,
            "metrics": selected,
            "series": series,
        }

    @mcp.tool()
    @api_tool
    async def compare_periods(
        window_days: Annotated[int, Field(ge=1, le=365)] = 7,
        gap_days: Annotated[int, Field(ge=0, le=365)] = 0,
        metrics: list[str] | None = None,
        group_by: str = "none",
    ) -> dict[str, Any]:
        """Compare two consecutive rolling windows. Period A = last
        `window_days` (ending today). Period B = same length, shifted back by
        `window_days + gap_days`."""
        if group_by not in GROUP_BY_OPTIONS:
            return bad_request(f"group_by must be one of {list(GROUP_BY_OPTIONS)}")
        selected = _select_metrics(metrics)

        today = date.today()
        a_to = today
        a_from = today - timedelta(days=window_days - 1)
        b_to = a_from - timedelta(days=1 + gap_days)
        b_from = b_to - timedelta(days=window_days - 1)

        def _start(d: date) -> datetime:
            return datetime.combine(d, time.min)

        def _end_exclusive(d: date) -> datetime:
            return datetime.combine(d + timedelta(days=1), time.min)

        # Two range queries instead of one spanning the gap — when gap_days
        # is non-trivial we'd otherwise fetch (and discard) the gap window.
        logs_a, logs_b = await asyncio.gather(
            _fetch_logs(api, limit=5000, since=_start(a_from), until=_end_exclusive(a_to)),
            _fetch_logs(api, limit=5000, since=_start(b_from), until=_end_exclusive(b_to)),
        )

        ex_cache = await _load_ex_meta(api, logs_a + logs_b, group_by)
        per_period: dict[str, dict[tuple | None, dict[str, float]]] = {
            "a": defaultdict(_new_metric_bucket),
            "b": defaultdict(_new_metric_bucket),
        }
        totals: dict[str, dict[str, float]] = {
            "a": _new_metric_bucket(),
            "b": _new_metric_bucket(),
        }
        for period, logs in (("a", logs_a), ("b", logs_b)):
            for entry in logs:
                ex_id = entry.get("exercise")
                if ex_id is None:
                    continue
                weight = _safe_float(entry.get("weight"))
                reps = _entry_reps(entry)
                _accumulate(totals[period], reps, weight)
                for group in _groups_for(ex_id, group_by, ex_cache):
                    _accumulate(per_period[period][group], reps, weight)

        all_groups: set[tuple | None] = set(per_period["a"].keys()) | set(per_period["b"].keys())

        comparison: list[dict[str, Any]] = []
        for group in all_groups:
            a = per_period["a"].get(group) or _new_metric_bucket()
            b = per_period["b"].get(group) or _new_metric_bucket()
            row: dict[str, Any] = {}
            if group_by != "none" and group is not None:
                row["group"] = {"key": group[0], "label": group[1]}
            row["a"] = _project(a, selected)
            row["b"] = _project(b, selected)
            row["delta"] = _delta(a, b, selected)
            row["delta_pct"] = _delta_pct(a, b, selected)
            comparison.append(row)
        comparison.sort(
            key=lambda r: abs(r["delta"].get("volume_kg") or 0),
            reverse=True,
        )

        return {
            "period_a": {"from": a_from.isoformat(), "to": a_to.isoformat()},
            "period_b": {"from": b_from.isoformat(), "to": b_to.isoformat()},
            "group_by": group_by,
            "metrics": selected,
            "total_a": _project(totals["a"], selected),
            "total_b": _project(totals["b"], selected),
            "total_delta": _delta(totals["a"], totals["b"], selected),
            "total_delta_pct": _delta_pct(totals["a"], totals["b"], selected),
            "comparison": comparison,
        }
