"""Shared helpers for tool modules."""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from typing import Any, Protocol, TypeVar
from uuid import UUID

import httpx
from wger_api_client.api.language import language_list
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
# fixture name lowercased. A log or a planned set that leaves this alone counts
# repetitions; the other units are what make a plank, a row or a run something
# other than "60 reps".
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


def as_weight_unit(unit: str | None) -> int | None:
    """Look up wger's id for 'kg' or 'lb'. ``None`` stays ``None``."""
    if unit is None:
        return None
    try:
        return WEIGHT_UNITS[unit]
    except KeyError:
        raise ToolInputError(
            f"unknown weight_unit '{unit}'; expected one of {', '.join(WEIGHT_UNITS)}"
        ) from None


def as_repetition_unit(unit: str | None) -> int | None:
    """Look up wger's id for a repetition unit. ``None`` stays ``None``."""
    if unit is None:
        return None
    try:
        return REPETITION_UNITS[unit]
    except KeyError:
        raise ToolInputError(
            f"unknown repetition unit '{unit}'; expected one of {', '.join(REPETITION_UNITS)}"
        ) from None


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


def at_noon(when: date | datetime | None) -> datetime | None:
    """Anchor a bare date at :data:`_BARE_DATE_TIME`.

    A ``datetime`` passes through unchanged, offset included, and ``None``
    stays ``None`` so the caller can leave the field to wger. Note ``datetime``
    is a subclass of ``date``, so the subclass is checked first.
    """
    if when is None or isinstance(when, datetime):
        return when
    return datetime.combine(when, _BARE_DATE_TIME)


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
