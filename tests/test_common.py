"""The shared tool helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from wger_api_client.types import UNSET

from wger_mcp.tools.common import (
    ToolInputError,
    as_decimal,
    as_int,
    as_uuid,
    at_noon,
    day_bounds,
    day_range_filters,
    opt,
    require_fields,
)

# ---------- at_noon ----------


def test_none_stays_none() -> None:
    """Omitting the field lets wger apply its own timezone.now default."""
    assert at_noon(None) is None


def test_bare_date_is_anchored_at_noon() -> None:
    assert at_noon(date(2026, 7, 21)) == datetime(2026, 7, 21, 12, 0)


def test_datetime_offset_is_preserved() -> None:
    """The reporter's case in issue #5: an explicit offset survives verbatim."""
    tz = timezone(timedelta(hours=2))
    stamp = at_noon(datetime(2026, 7, 21, 7, 0, tzinfo=tz))
    assert stamp == datetime(2026, 7, 21, 7, 0, tzinfo=tz)


def test_naive_datetime_keeps_its_time() -> None:
    assert at_noon(datetime(2026, 7, 21, 7, 30)) == datetime(2026, 7, 21, 7, 30)


def test_datetime_checked_before_date() -> None:
    """datetime subclasses date, so a naive isinstance order would truncate."""
    assert at_noon(datetime(2026, 7, 21, 23, 45)) != datetime(2026, 7, 21, 12, 0)


# ---------- opt ----------


def test_only_none_becomes_unset() -> None:
    """0 and False are values the caller chose, not omissions."""
    assert opt(None) is UNSET
    assert opt(0) == 0
    assert opt(False) is False
    assert opt("") == ""


# ---------- ids and decimals ----------


def test_ids_are_parsed_or_refused() -> None:
    assert as_int("42", "log_id") == 42
    assert str(as_uuid("018f6f30-0000-7000-8000-000000000001", "plan_id")).startswith("018f6f30")
    with pytest.raises(ToolInputError, match="plan_id"):
        as_uuid("nope", "plan_id")
    with pytest.raises(ToolInputError, match="numeric id"):
        as_int("nope", "exercise_id")


def test_decimals_travel_without_trailing_zeros() -> None:
    assert as_decimal(82.5) == "82.5"
    assert as_decimal(90) == "90"


# ---------- require_fields ----------


def test_empty_patch_is_refused() -> None:
    class _Body:
        def __init__(self, data: dict) -> None:
            self._data = data

        def to_dict(self) -> dict:
            return self._data

    require_fields(_Body({"name": "x"}))
    with pytest.raises(ToolInputError, match="no fields to update"):
        require_fields(_Body({}))


# ---------- day_bounds / day_range_filters ----------


def test_range_covers_the_whole_last_day() -> None:
    """The bound wger filters on is exclusive, so it has to land on the day
    after the one the caller asked for — anything else drops the final day."""
    since, until = day_bounds(date(2026, 8, 1), date(2026, 8, 18))
    assert since == datetime(2026, 8, 1, 0, 0)
    assert until == datetime(2026, 8, 19, 0, 0)


def test_a_single_day_is_a_whole_day() -> None:
    since, until = day_bounds(date(2026, 8, 18), date(2026, 8, 18))
    assert since == datetime(2026, 8, 18, 0, 0)
    assert until == datetime(2026, 8, 19, 0, 0)


def test_open_ends_stay_open() -> None:
    assert day_bounds(None, None) == (None, None)
    assert day_bounds(date(2026, 8, 1), None)[1] is None
    assert day_bounds(None, date(2026, 8, 1))[0] is None


def test_filters_leave_out_what_was_not_asked_for() -> None:
    """An absent key, not UNSET: an unfiltered call sends no date query."""
    assert day_range_filters(None, None) == {}
    assert set(day_range_filters(date(2026, 8, 1), None)) == {"date_gte"}
    assert set(day_range_filters(None, date(2026, 8, 1))) == {"date_lt"}
    assert set(day_range_filters(date(2026, 8, 1), date(2026, 8, 2))) == {
        "date_gte",
        "date_lt",
    }
