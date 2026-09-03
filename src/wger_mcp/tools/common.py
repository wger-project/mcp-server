"""Shared helpers for tool modules."""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol, TypeVar
from uuid import UUID

import httpx
from wger_api_client.api.language import language_list
from wger_api_client.api.userprofile import userprofile_retrieve
from wger_api_client.client import AuthenticatedClient
from wger_api_client.errors import UnexpectedStatus
from wger_api_client.types import UNSET, Unset

from ..api_client import paginate

T = TypeVar("T")

# A bare date has to land somewhere on the day wger stores as a timestamp;
# noon keeps it on the intended day in either direction of a timezone shift.
_BARE_DATE_TIME = time(12, 0)

# wger's weight-unit ids (/api/v2/setting-weightunit/). Used by logging and by
# routine authoring, which record the unit in different places: a workout log
# carries its own weight_unit, while a planned set takes it from its slot entry.
WEIGHT_UNITS: dict[str, int] = {"kg": 1, "lb": 2}
_WEIGHT_UNIT_NAMES: dict[int, str] = {v: k for k, v in WEIGHT_UNITS.items()}

# wger's repetition-unit ids (/api/v2/setting-repetitionunit/), keyed by the
# fixture name lowercased. The ids are NOT in any natural order — seconds is 3
# and until_failure is 2 — so a caller must look a name up here rather than
# infer a number from the order it saw the units listed in. A log or a planned
# set that leaves this alone counts repetitions; the other units are what make a
# plank, a row or a run something other than "60 reps".
REPETITION_UNITS: dict[str, int] = {
    "repetitions": 1,
    "until_failure": 2,
    "seconds": 3,
    "minutes": 4,
    "miles": 5,
    "kilometers": 6,
    "max_reps": 7,
    "meters": 8,
}


# wger accepts RiR only in half steps up to 4.5 (manager/consts.py
# RIR_OPTIONS), enforced by a model validator that DRF carries onto the
# serializer field. A looser bound here only spends a round trip on a 400.
RIR_MAX = 4.5
RIR_STEP = 0.5


class ToolInputError(Exception):
    """An argument wger cannot accept. Reported to the caller as a 400."""


def bad_request(detail: str) -> dict[str, Any]:
    """Shape a 400-style validation error as a tool-response dict."""
    return {"error": True, "status": 400, "detail": detail}


def api_err(exc: UnexpectedStatus | httpx.HTTPError) -> dict[str, Any]:
    """Shape an upstream failure as a tool-response dict."""
    if isinstance(exc, UnexpectedStatus):
        try:
            detail: Any = json.loads(exc.content)
        except ValueError:
            detail = exc.content.decode(errors="replace")
        return {"error": True, "status": exc.status_code, "detail": detail}
    return {"error": True, "status": 503, "detail": f"wger is unreachable: {exc}"}


def opt(value: T | None) -> T | Unset:
    """What the caller left out stays out of the request."""
    return UNSET if value is None else value


def as_uuid(value: str, field: str) -> UUID:
    """Parse an opaque id from the tool boundary into the UUID the API wants."""
    try:
        return UUID(value)
    except ValueError:
        raise ToolInputError(f"{field} must be a UUID, got {value!r}") from None


def as_int(value: str, field: str) -> int:
    """Parse an opaque id from the tool boundary into the int the API wants."""
    try:
        return int(value)
    except ValueError:
        raise ToolInputError(f"{field} must be a numeric id, got {value!r}") from None


def as_decimal(value: float) -> str:
    """Decimal fields travel as strings in the API."""
    return f"{value:g}"


# The optional halves of the three above, for the arguments a patch may leave
# out. Parsing and "absent stays absent" belong together: spelled apart, every
# call site repeats the None check, and one of them eventually gets it backwards.


def opt_uuid(value: str | None, field: str) -> UUID | Unset:
    """:func:`as_uuid`, or ``UNSET`` when the caller left the argument out."""
    return UNSET if value is None else as_uuid(value, field)


def opt_int(value: str | None, field: str) -> int | Unset:
    """:func:`as_int`, or ``UNSET`` when the caller left the argument out."""
    return UNSET if value is None else as_int(value, field)


def opt_decimal(value: float | None) -> str | Unset:
    """:func:`as_decimal`, or ``UNSET`` when the caller left the argument out."""
    return UNSET if value is None else as_decimal(value)


def _unit_id(
    unit: int | str | None, units: dict[str, int], field: str, allow_id: bool
) -> int | None:
    """Resolve a unit name to wger's id. ``None`` and a numeric id stay as they are.

    Names are matched loosely, because wger's own display names ('Until
    Failure', 'Seconds') are what a caller has most likely seen. Loosening a
    name can only turn a refusal into the right unit.

    ``allow_id`` says whether a digit string means that id. Only the tools
    whose parameter is typed ``int | str`` set it: there pydantic's smart union
    keeps ``"3"`` a string instead of coercing it as a bare ``int`` would, and
    refusing it would drop a case that worked before. Where the parameter is
    typed ``str``, a number was never accepted, so it stays refused — turning
    it into an id there would be a new way to write the wrong unit silently.
    """
    if unit is None or isinstance(unit, int):
        return unit
    name = unit.strip().lower().replace(" ", "_")
    if allow_id and name.isdigit():
        return int(name)
    try:
        return units[name]
    except KeyError:
        expected = ", ".join(units)
        raise ToolInputError(
            f"unknown {field} '{unit}'; expected one of {expected}"
            + (", or wger's numeric id" if allow_id else "")
        ) from None


def as_weight_unit(unit: int | str | None, allow_id: bool = False) -> int | None:
    """Look up wger's id for 'kg' or 'lb'. ``None`` stays ``None``."""
    return _unit_id(unit, WEIGHT_UNITS, "weight_unit", allow_id)


def as_repetition_unit(unit: int | str | None, allow_id: bool = False) -> int | None:
    """Look up wger's id for a repetition unit. ``None`` stays ``None``."""
    return _unit_id(unit, REPETITION_UNITS, "repetition unit", allow_id)


def weight_unit_name(unit_id: Any) -> Any:
    """Render wger's numeric weight unit as its code, for data going out.

    A plan is read by people and by language models, and ``weight_unit: 1``
    invites both to guess — one assistant reported a 14 kg set as "14 lb".
    Units this server does not name (Body Weight, Plates, km/h, ...) pass
    through unchanged rather than being labelled wrongly.
    """
    return _WEIGHT_UNIT_NAMES.get(unit_id, unit_id)


def language_id_resolver(api: AuthenticatedClient) -> Callable[[str], Awaitable[int | None]]:
    """A cached lookup of wger's numeric id for a language code.

    Exercise names live on translations, which carry the language as an id, so
    picking the name in the language the caller asked for needs this mapping.
    Cached per resolver: the language table is static. A failed lookup returns
    ``None`` and is not cached, leaving the caller to fall back to whatever
    translation comes first instead of failing.
    """
    cache: dict[str, int | None] = {}

    async def resolve(code: str) -> int | None:
        if code not in cache:
            try:
                rows = await paginate(language_list.asyncio, client=api, limit=5, short_name=code)
            except (UnexpectedStatus, httpx.HTTPError):
                return None
            cache[code] = next(
                (r.get("id") for r in rows if isinstance(r, dict) and r.get("id")), None
            )
        return cache[code]

    return resolve


async def profile_weight_unit(api: AuthenticatedClient) -> str:
    """The authenticated trainee's own weight unit, from their wger profile.

    A caller that omits the unit should get the unit the trainee actually works
    in rather than a fixed metric default. A profile that says ``lb`` and a
    reported "225" means 225 pounds; storing that as 225 kilograms is wrong by a
    factor of 2.2, and nothing downstream can tell, because the number is
    plausible either way.

    Deliberately not cached, and not a per-registration closure: one shared
    client serves every user (see :mod:`..api_client`), so a cache here would
    pin the first trainee's unit onto every other trainee's writes.

    A unit that cannot be read refuses the write instead of standing in ``kg``.
    The guess is unrecoverable once stored — the row does not say what was meant
    — while the refusal costs one retry with an explicit ``weight_unit``. An
    unreachable wger or an error status propagates to :func:`api_tool`; a reply
    that will not parse into a unit is a :class:`ToolInputError`, because naming
    the unit is something the caller can do. ``Userprofile.from_dict`` raises
    ``TypeError`` for a unit outside ``{kg, lb}`` (``check_weight_unit_enum``),
    ``KeyError`` for a missing required field, ``ValueError`` for a bad
    ``date_joined`` or a body that is not JSON.
    """
    try:
        profile = await userprofile_retrieve.asyncio(client=api)
    except (TypeError, KeyError, ValueError) as exc:
        raise ToolInputError(
            f"the trainee's wger profile could not be read ({exc}); "
            "pass weight_unit to say which unit the weight is in"
        ) from exc
    unit = profile.weight_unit if profile is not None else None
    if unit not in WEIGHT_UNITS:
        raise ToolInputError(
            f"the trainee's wger profile names no weight unit this server knows ({unit!r}); "
            "pass weight_unit to say which unit the weight is in"
        )
    return unit


def at_noon(when: date | datetime | None) -> datetime | None:
    """Anchor a bare date at :data:`_BARE_DATE_TIME`.

    A ``datetime`` passes through unchanged, offset included, and ``None``
    stays ``None`` so the caller can leave the field to wger. Note ``datetime``
    is a subclass of ``date``, so the subclass is checked first.
    """
    if when is None or isinstance(when, datetime):
        return when
    return datetime.combine(when, _BARE_DATE_TIME)


def day_bounds(first: date | None, last: date | None) -> tuple[datetime | None, datetime | None]:
    """The half-open timestamp range covering the days ``first``..``last``.

    Both ends are inclusive whole days at the tool boundary, while wger stores
    these as timestamps and filters them with ``date_gte`` / ``date_lt``. The
    upper bound is therefore midnight *after* ``last``: filtering on ``last``
    itself would silently drop everything recorded on the final day, which is
    the one nobody checks. Either end may be ``None``, leaving that side open.
    """
    since = None if first is None else datetime.combine(first, time.min)
    until = None if last is None else datetime.combine(last + timedelta(days=1), time.min)
    return since, until


def day_range_filters(first: date | None, last: date | None) -> dict[str, datetime]:
    """:func:`day_bounds` as list-endpoint filters, an open end left out.

    Absent rather than ``UNSET``, so an unfiltered call sends no date query at
    all.
    """
    since, until = day_bounds(first, last)
    filters: dict[str, datetime] = {}
    if since is not None:
        filters["date_gte"] = since
    if until is not None:
        filters["date_lt"] = until
    return filters


class _Body(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


def require_fields(body: _Body) -> None:
    """Refuse a patch that would send nothing."""
    if not body.to_dict():
        raise ToolInputError("no fields to update")


def api_tool(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Turn a rejected argument or an upstream failure into an error dict.

    Only :class:`ToolInputError` counts as an argument problem, so a parse
    error on the response is not mistaken for one.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except ToolInputError as exc:
            return bad_request(str(exc))
        except (UnexpectedStatus, httpx.HTTPError) as exc:
            return api_err(exc)

    return wrapper


def api_list_tool(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """:func:`api_tool` for tools whose result is a list."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except ToolInputError as exc:
            return [bad_request(str(exc))]
        except (UnexpectedStatus, httpx.HTTPError) as exc:
            return [api_err(exc)]

    return wrapper
