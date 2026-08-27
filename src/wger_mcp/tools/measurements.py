"""Body measurement tools (categories + entries), via the generated
``wger_api_client``."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from wger_api_client import models as api_models
from wger_api_client.api.measurement import (
    measurement_aggregate_list,
    measurement_create,
    measurement_destroy,
    measurement_list,
    measurement_partial_update,
    measurement_retrieve,
)
from wger_api_client.api.measurement_category import (
    measurement_category_create,
    measurement_category_destroy,
    measurement_category_list,
    measurement_category_partial_update,
    measurement_category_retrieve,
)
from wger_api_client.client import AuthenticatedClient
from wger_api_client.models.measurement_aggregate_list_bucket import (
    MEASUREMENT_AGGREGATE_LIST_BUCKET_VALUES as BUCKETS,
)
from wger_api_client.models.metric_type_enum import METRIC_TYPE_ENUM_VALUES
from wger_api_client.models.source_enum import SOURCE_ENUM_VALUES
from wger_api_client.types import UNSET

from ..api_client import paginate
from ..config import Settings
from .common import (
    MEASUREMENT_VALUE_MAX,
    ToolInputError,
    api_list_tool,
    api_tool,
    as_uuid,
    at_noon,
    bad_request,
    opt,
    require_fields,
)

# Model field limits, so the caller is told before the server refuses
CATEGORY_NAME_MAX = 100
CATEGORY_UNIT_MAX = 30
NOTES_MAX = 100


# The unit each metric type is conventionally kept in, used only to default one
# for a category being created. wger has no server-side table for this, so these
# follow what its own dummy generator writes
# (measurements/management/commands/dummy-generator-health-measurements.py) and
# what the health importer stores. A convenience, not a contract: the unit is a
# free-text column, nothing validates it, and update_measurement_category can
# change it afterwards. Body weight is the one exception wger does check, where
# only kg and lb are accepted.
DEFAULT_UNITS: dict[str, str] = {
    "body_weight": "kg",
    "body_fat": "%",
    "lean_body_mass": "kg",
    "height": "cm",
    "blood_pressure": "mmHg",
    "blood_pressure_systolic": "mmHg",
    "blood_pressure_diastolic": "mmHg",
    "heart_rate": "bpm",
    "resting_heart_rate": "bpm",
    "blood_oxygen": "%",
    "steps": "count",
    "distance": "km",
    "energy": "kcal",
    "sleep": "min",
    "sleep_total": "min",
    "sleep_light": "min",
    "sleep_deep": "min",
    "sleep_rem": "min",
    "sleep_awake": "min",
}


def as_metric_type(metric_type: str) -> str:
    """Check a metric type against the ones the schema knows.

    Taken from the generated client rather than spelled out here, so the set
    follows the server on the next regeneration instead of drifting.
    """
    if metric_type not in METRIC_TYPE_ENUM_VALUES:
        raise ToolInputError(
            f"unknown metric_type '{metric_type}'; "
            f"expected one of {', '.join(sorted(METRIC_TYPE_ENUM_VALUES))}"
        )
    return metric_type


def as_source(source: str) -> str:
    """Check an entry origin against the ones the schema knows."""
    if source not in SOURCE_ENUM_VALUES:
        raise ToolInputError(
            f"unknown source '{source}'; expected one of {', '.join(sorted(SOURCE_ENUM_VALUES))}"
        )
    return source


SYSTOLIC = "blood_pressure_systolic"
DIASTOLIC = "blood_pressure_diastolic"


def register(mcp: FastMCP, api: AuthenticatedClient, settings: Settings) -> None:
    async def _categories(**filters: Any) -> list[dict[str, Any]]:
        return await paginate(measurement_category_list.asyncio, client=api, limit=50, **filters)

    async def _blood_pressure_components() -> tuple[str, str]:
        """Return the (systolic, diastolic) category ids, building them if the
        trainee has never recorded a blood pressure.

        A reading is two rows, one per component category, and those live under
        a `blood_pressure` group. Creating the group creates its children, so
        the setup is one call — but it has to be found first, because a second
        group would be refused.
        """
        groups = await _categories(metric_type="blood_pressure")
        if groups:
            group_id = groups[0]["id"]
        else:
            created = await measurement_category_create.asyncio(
                client=api,
                body=api_models.CategoryRequest(
                    name="Blood pressure",
                    unit=DEFAULT_UNITS["blood_pressure"],
                    metric_type="blood_pressure",
                ),
            )
            group_id = created.to_dict()["id"]

        children = {c.get("metric_type"): c["id"] for c in await _categories(parent=group_id)}

        # A child can be missing if someone deleted it. wger rebuilds a group's
        # components on update as well as on create, so patching the group with
        # the name it already has puts them back rather than failing here.
        if SYSTOLIC not in children or DIASTOLIC not in children:
            await measurement_category_partial_update.asyncio(
                id=as_uuid(group_id, "category_id"),
                client=api,
                body=api_models.PatchedCategoryRequest(
                    name=groups[0]["name"] if groups else "Blood pressure"
                ),
            )
            children = {c.get("metric_type"): c["id"] for c in await _categories(parent=group_id)}

        try:
            return children[SYSTOLIC], children[DIASTOLIC]
        except KeyError:
            raise ToolInputError(
                "the blood pressure category has no systolic/diastolic components "
                "and they could not be rebuilt; check the category setup in wger"
            ) from None

    # ── Categories ──────────────────────────────────────────────────────────

    @mcp.tool()
    @api_list_tool
    async def list_measurement_categories(
        metric_type: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List body measurement categories, optionally of one metric type only.

        Since wger 2.7 a category has a `metric_type` that says what it holds,
        and the list is no longer only the free-form ones the trainee invented
        ('Waist', 'Bicep', … which are `custom`). Read these fields before
        writing to one:

        * `metric_type='blood_pressure'` or `'sleep'` is a **group**: a
          container that carries no entries of its own. Its readings go into
          the child categories that have it as their `parent` — systolic and
          diastolic, or the five sleep stages. Writing to the group is refused.
        * `is_official=True` marks a category wger itself depends on (body
          weight today). It cannot be deleted.
        * `dynamic_type` other than 'NONE' marks a **calculated** category, BMI
          for instance. The server maintains its entries; creating, editing or
          deleting one is refused.

        Everything else is a leaf and takes entries normally. Pass `metric_type`
        to go straight to one — 'body_weight', 'body_fat', 'height',
        'heart_rate', 'steps', 'sleep_rem' and so on — instead of reading the
        whole list to find it.
        """
        filters: dict[str, Any] = {}
        if metric_type is not None:
            filters["metric_type"] = as_metric_type(metric_type)
        return await paginate(measurement_category_list.asyncio, client=api, limit=limit, **filters)

    @mcp.tool()
    @api_tool
    async def create_measurement_category(
        name: Annotated[str, Field(min_length=1, max_length=CATEGORY_NAME_MAX)],
        unit: Annotated[str | None, Field(max_length=CATEGORY_UNIT_MAX)] = None,
        metric_type: str | None = None,
    ) -> dict[str, Any]:
        """Create a body measurement category.

        Without metric_type this is a free-form one, the tape-measure kind
        (name='Bicep', unit='cm'), and the unit defaults to cm.

        With metric_type the category is typed, which is what lets wger chart it
        properly and lets a health sync write into it: 'body_fat', 'height',
        'heart_rate', 'resting_heart_rate', 'blood_oxygen', 'steps', 'distance',
        'energy', 'lean_body_mass'. The unit then defaults to the conventional
        one for that type ('%', 'cm', 'bpm', 'count', 'km', 'kcal', …), so pass
        one only to override it.

        Three things wger enforces:

        * **One typed category per person.** A second one of the same type is
          refused; use list_measurement_categories(metric_type=…) to find the
          existing one. Body weight already exists for everyone.
        * **The type is fixed once set.** There is no changing it afterwards,
          and update_measurement_category cannot move a category into a type.
        * **Groups build themselves.** Creating metric_type='blood_pressure' or
          'sleep' also creates the child categories the readings actually go
          into. Do not create those children directly — they are refused
          without their parent. For blood pressure prefer log_blood_pressure,
          which sets the whole thing up on its own.
        """
        chosen = as_metric_type(metric_type) if metric_type is not None else None
        body = api_models.CategoryRequest(
            name=name,
            unit=unit or DEFAULT_UNITS.get(chosen or "", "cm"),
            metric_type=opt(chosen),
        )
        created = await measurement_category_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_tool
    async def get_measurement_category(category_id: str) -> dict[str, Any]:
        """Fetch a single measurement category by ID."""
        category = await measurement_category_retrieve.asyncio(
            id=as_uuid(category_id, "category_id"), client=api
        )
        return category.to_dict()

    @mcp.tool()
    @api_tool
    async def update_measurement_category(
        category_id: str,
        name: Annotated[str | None, Field(max_length=CATEGORY_NAME_MAX)] = None,
        unit: Annotated[str | None, Field(max_length=CATEGORY_UNIT_MAX)] = None,
    ) -> dict[str, Any]:
        """Rename or change the unit of a measurement category."""
        category = as_uuid(category_id, "category_id")
        body = api_models.PatchedCategoryRequest(name=opt(name), unit=opt(unit))
        require_fields(body)
        updated = await measurement_category_partial_update.asyncio(
            id=category, client=api, body=body
        )
        return updated.to_dict()

    @mcp.tool()
    @api_tool
    async def delete_measurement_category(category_id: str) -> dict[str, Any]:
        """Delete a measurement category and all its entries."""
        await measurement_category_destroy.asyncio_detailed(
            id=as_uuid(category_id, "category_id"), client=api
        )
        return {"deleted": True, "category_id": category_id}

    # ── Entries ──────────────────────────────────────────────────────────────

    @mcp.tool()
    @api_list_tool
    async def list_measurements(
        category_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        source: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List body measurement entries (newest first), optionally filtered by
        category, date range and origin. Both dates are inclusive.

        Without a range a growing history has to be pulled in full and trimmed
        by the caller, which spends context on entries nobody asked for; for a
        trend rather than the entries themselves, summarize_measurements is far
        cheaper.

        source filters on where an entry came from: 'user' is hand-entered,
        'apple' and 'google' come from a phone's health sync, 'calculated' is
        maintained by wger itself (BMI and the like). Every entry carries it,
        so a reading synced from a watch is distinguishable from one the
        trainee typed — worth checking before treating a number as deliberate.
        """
        filters: dict[str, Any] = {"ordering": "-date"}
        if category_id is not None:
            filters["category"] = as_uuid(category_id, "category_id")
        if source is not None:
            filters["source"] = as_source(source)
        if date_from is not None:
            filters["date_gte"] = datetime.combine(date_from, time.min)
        if date_to is not None:
            filters["date_lt"] = datetime.combine(date_to + timedelta(days=1), time.min)
        return await paginate(measurement_list.asyncio, client=api, limit=limit, **filters)

    @mcp.tool()
    @api_tool
    async def log_measurement(
        category_id: str,
        value: Annotated[float, Field(ge=0, le=MEASUREMENT_VALUE_MAX)],
        when: date | datetime | None = None,
        notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None,
    ) -> dict[str, Any]:
        """Add a body measurement entry to a category. Defaults to now; a bare
        date lands at 12:00."""
        body = api_models.MeasurementRequest(
            category=as_uuid(category_id, "category_id"),
            value=value,
            date=opt(at_noon(when)),
            notes=opt(notes),
            # The generated model defaults this to 'user' rather than leaving it
            # out, so it has to be unset explicitly. See update_measurement for
            # why sending it would be worse than pointless.
            source=UNSET,
        )
        created = await measurement_create.asyncio(client=api, body=body)
        return created.to_dict()

    @mcp.tool()
    @api_list_tool
    async def summarize_measurements(
        category_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        bucket: str = "auto",
        max_points: Annotated[int, Field(ge=1, le=1000)] = 200,
        timezone_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Condense measurement entries into per-period rows: one per category,
        calendar bucket and unit, with count, sum, min and max.

        This is what to reach for when the question is a trend rather than the
        entries themselves — "how has my weight moved this year", "how many
        steps a day on average". A year of daily weigh-ins is 365 entries
        through list_measurements and twelve rows through this.

        bucket is 'auto' (the default, which picks the finest period keeping the
        series under max_points), or one of 'hour', 'day', 'week', 'month'.

        Buckets are cut in the trainee's own calendar, taken from their wger
        profile, so a reading just after midnight counts for the day they had
        it. timezone_name overrides that with an IANA name ('Europe/Berlin').

        Note the rows split by unit as well: a body weight logged partly in kg
        and partly in lb gives two rows per period, which is why each carries
        its own unit rather than the category's.
        """
        if bucket not in BUCKETS:
            raise ToolInputError(
                f"unknown bucket '{bucket}'; expected one of {', '.join(sorted(BUCKETS))}"
            )
        filters: dict[str, Any] = {"bucket": bucket, "max_points": max_points}
        if category_id is not None:
            filters["category"] = as_uuid(category_id, "category_id")
        if date_from is not None:
            filters["date_gte"] = datetime.combine(date_from, time.min)
        if date_to is not None:
            filters["date_lt"] = datetime.combine(date_to + timedelta(days=1), time.min)
        if timezone_name is not None:
            filters["tz"] = timezone_name
        # Not paginated: the endpoint answers with the whole series in one go,
        # which is the point of having condensed it. paginate() would look for
        # a `results` envelope that is not there.
        rows = await measurement_aggregate_list.asyncio(client=api, **filters)
        return [row.to_dict() for row in rows or []]

    @mcp.tool()
    @api_tool
    async def get_measurement(measurement_id: str) -> dict[str, Any]:
        """Fetch a single body measurement entry by ID."""
        entry = await measurement_retrieve.asyncio(
            id=as_uuid(measurement_id, "measurement_id"), client=api
        )
        return entry.to_dict()

    @mcp.tool()
    @api_tool
    async def update_measurement(
        measurement_id: str,
        value: Annotated[float | None, Field(ge=0, le=MEASUREMENT_VALUE_MAX)] = None,
        when: date | datetime | None = None,
        notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None,
        category_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch a body measurement entry.

        category_id moves the entry to another category, for one filed under
        the wrong one — otherwise the only remedy is to delete it and log it
        again, which loses nothing but costs the original date.
        """
        entry = as_uuid(measurement_id, "measurement_id")
        body = api_models.PatchedMeasurementRequest(
            value=opt(value),
            date=opt(at_noon(when)),
            notes=opt(notes),
            category=opt(as_uuid(category_id, "category_id") if category_id is not None else None),
            # Never send the source on a patch: it would restamp a health-synced
            # entry as hand-entered and lose the provenance the importer wrote,
            # and it would make every patch non-empty, walking straight past
            # require_fields below. wger 2.7 stopped putting a default on this
            # field in the PATCH schema, so the generated model no longer fills
            # it in — this keeps us correct against the builds that still do.
            source=UNSET,
        )
        require_fields(body)
        updated = await measurement_partial_update.asyncio(id=entry, client=api, body=body)
        return updated.to_dict()

    # ── Blood pressure ───────────────────────────────────────────────────────

    @mcp.tool()
    @api_tool
    async def log_blood_pressure(
        systolic: Annotated[float, Field(ge=0, le=MEASUREMENT_VALUE_MAX)],
        diastolic: Annotated[float, Field(ge=0, le=MEASUREMENT_VALUE_MAX)],
        when: date | datetime | None = None,
        notes: Annotated[str | None, Field(max_length=NOTES_MAX)] = None,
    ) -> dict[str, Any]:
        """Record one blood pressure reading. Defaults to now; a bare date lands
        at 12:00.

        wger stores a reading as two entries, systolic and diastolic, in two
        child categories of a 'blood_pressure' group, paired by carrying the
        exact same timestamp. This writes both and sets up the categories the
        first time — doing it through log_measurement instead means finding two
        categories and matching timestamps by hand, and a pair that drifts apart
        by a second is no longer one reading.

        The usual bounds are 50-250 systolic and 30-150 diastolic; wger refuses
        anything outside and names the range.
        """
        if systolic <= diastolic:
            return bad_request(
                f"systolic ({systolic}) must be above diastolic ({diastolic}); "
                "they may have been passed the wrong way round"
            )

        systolic_id, diastolic_id = await _blood_pressure_components()
        # One timestamp for both: that identity is what makes them a reading.
        # Letting the server default each would set them microseconds apart.
        stamp = at_noon(when) or datetime.now(UTC)

        written = []
        for category_id, value in ((systolic_id, systolic), (diastolic_id, diastolic)):
            created = await measurement_create.asyncio(
                client=api,
                body=api_models.MeasurementRequest(
                    category=as_uuid(category_id, "category_id"),
                    value=value,
                    date=stamp,
                    notes=opt(notes),
                    source=UNSET,
                ),
            )
            written.append(created.to_dict()["id"])

        return {
            "date": stamp.isoformat(),
            "systolic": systolic,
            "diastolic": diastolic,
            "measurement_ids": written,
        }

    @mcp.tool()
    @api_list_tool
    async def get_blood_pressure_history(
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 30,
    ) -> list[dict[str, Any]]:
        """Return blood pressure readings (newest first), each as one row with
        both numbers. Both dates are inclusive.

        The two halves of a reading are stored as separate entries sharing a
        timestamp, so reading them through list_measurements gives two
        unrelated-looking series. This puts them back together. A half without
        its counterpart is still reported, with the missing side null, rather
        than dropped.
        """
        systolic_id, diastolic_id = await _blood_pressure_components()
        filters: dict[str, Any] = {
            "ordering": "-date",
            "category_in": [
                as_uuid(systolic_id, "category_id"),
                as_uuid(diastolic_id, "category_id"),
            ],
        }
        if date_from is not None:
            filters["date_gte"] = datetime.combine(date_from, time.min)
        if date_to is not None:
            filters["date_lt"] = datetime.combine(date_to + timedelta(days=1), time.min)

        # Two entries per reading, so ask for enough of them to fill `limit`
        entries = await paginate(measurement_list.asyncio, client=api, limit=limit * 2, **filters)

        readings: dict[str, dict[str, Any]] = {}
        for entry in entries:
            stamp = entry.get("date")
            reading = readings.setdefault(
                stamp, {"date": stamp, "systolic": None, "diastolic": None, "notes": None}
            )
            side = "systolic" if str(entry.get("category")) == str(systolic_id) else "diastolic"
            reading[side] = entry.get("value")
            reading["notes"] = reading["notes"] or entry.get("notes") or None
        return list(readings.values())[:limit]

    @mcp.tool()
    @api_tool
    async def delete_measurement(measurement_id: str) -> dict[str, Any]:
        """Delete a body measurement entry."""
        await measurement_destroy.asyncio_detailed(
            id=as_uuid(measurement_id, "measurement_id"), client=api
        )
        return {"deleted": True, "measurement_id": measurement_id}
