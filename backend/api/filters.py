"""Filters API — returns distinct values for filter dropdowns.

When called without params, returns cached unfiltered values.
When called with filter params, returns filtered values (no cache).
"""

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Query

from backend.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory cache (unfiltered only)
_cache: dict | None = None
_cache_ts: float = 0.0


DOD_DEPARTMENTS = [
    "Department of Defense",
    "Department of the Army",
    "Department of the Navy",
    "Department of the Air Force",
]


NCR_LOCALITY = "Washington-Baltimore-Arlington, DC-MD-VA-WV-PA"
# Doctoral/licensed clinical providers (physicians, behavioral-health, therapists,
# PA/pharmacist, etc.); exclude_providers hides these and keeps support/techs/admin.
CLINICAL_PROVIDER_SERIES = (
    "('0180','0182','0183','0185','0602','0603','0630','0631','0633','0638',"
    "'0651','0660','0662','0665','0667','0668','0680','0682','0701')"
)


def _content_clause(field: str, value, params: list) -> str | None:
    """Emit one WHERE fragment for a content filter, binding params via $N.

    None when the field is unset or matches nothing to add; the shared builder
    skips those. Both /api/filters and the jobs endpoints route through here so
    the field-by-field SQL lives in exactly one place.
    """
    match field, value:
        case _, None | False | "":
            return None
        case "department", "Department of Defense (All)":
            start = len(params)
            params.extend(DOD_DEPARTMENTS)
            placeholders = ", ".join(f"${i}" for i in range(start + 1, len(params) + 1))
            return f"department IN ({placeholders})"
        case "department", _:
            params.append(value)
            return f"department = ${len(params)}"
        case "agency", _:
            params.append(value)
            return f"org = ${len(params)}"
        case "clearance", _:
            params.append(value)
            return f"clearance = ${len(params)}"
        case "state", "Remote":
            return "remote = true"
        case "state", "Telework Eligible":
            return "telework = true"
        case "state", _:
            params.append(value)
            return f"state = ${len(params)}"
        case "country", _:
            params.append(value)
            return f"country = ${len(params)}"
        case "city", _:
            params.append(value)
            return f"location_name = ${len(params)}"
        case "keyword", _:
            params.append(value)
            return (
                f"to_tsvector('english', title) "
                f"@@ plainto_tsquery('english', ${len(params)})"
            )
        case "grade_min", _:
            params.append(value)
            return f"gs_max >= ${len(params)}"
        case "grade_max", _:
            params.append(value)
            return f"gs_min <= ${len(params)}"
        case "salary_min", _:
            params.append(str(value))
            return f"max_salary::numeric >= ${len(params)}::numeric"
        case "salary_max", _:
            params.append(str(value))
            return f"min_salary::numeric <= ${len(params)}::numeric"
        case "series", _:
            params.append(value)
            return f"series_code = ${len(params)}"
        case "locality", _:
            params.append(value)
            return f"locality_area = ${len(params)}"
        case "exclude_ncr", _:
            params.append(NCR_LOCALITY)
            return f"(locality_area IS DISTINCT FROM ${len(params)})"
        case "exclude_registers", _:
            return "NOT is_evergreen"
        case "exclude_providers", _:
            return f"COALESCE(series_code, '') NOT IN {CLINICAL_PROVIDER_SERIES}"


def build_content_where(
    order: tuple[str, ...], values: dict, clauses: list[str], params: list
) -> tuple[str, list]:
    """Apply content filters in the caller's field order onto seeded clauses/params.

    `clauses`/`params` may already hold a bbox fragment ($1..$4); content
    placeholders continue from there.
    """
    for field in order:
        if (clause := _content_clause(field, values.get(field), params)) is not None:
            clauses.append(clause)
    return " AND ".join(clauses), params


_WHERE_ORDER = (
    "department",
    "agency",
    "clearance",
    "state",
    "country",
    "city",
    "keyword",
    "grade_min",
    "grade_max",
    "salary_min",
    "salary_max",
    "series",
    "locality",
    "exclude_ncr",
    "exclude_registers",
    "exclude_providers",
)


def _build_where(
    department=None,
    agency=None,
    clearance=None,
    state=None,
    country=None,
    city=None,
    keyword=None,
    grade_min=None,
    grade_max=None,
    salary_min=None,
    salary_max=None,
    series=None,
    locality=None,
    exclude_ncr=False,
    exclude_registers=False,
    exclude_providers=False,
    bbox=None,
) -> tuple[str, list]:
    """Build WHERE clause and params for filtered filter-dropdown queries."""
    clauses: list[str] = []
    params: list = []
    if bbox is not None:
        west, south, east, north = bbox
        clauses.append("geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)")
        params.extend([west, south, east, north])
    where, params = build_content_where(_WHERE_ORDER, locals(), clauses, params)
    return (where or "TRUE"), params


async def _load_filters(conn, where="TRUE", params=None) -> dict:
    """Query distinct filter values from jobs_geo, optionally filtered."""
    if params is None:
        params = []

    departments = await conn.fetch(
        f"SELECT department, COUNT(*) AS c FROM jobs_geo "
        f"WHERE department IS NOT NULL AND ({where}) "
        f"GROUP BY department ORDER BY c DESC",
        *params,
    )
    agencies = await conn.fetch(
        f"SELECT org, COUNT(*) AS c FROM jobs_geo "
        f"WHERE org IS NOT NULL AND ({where}) "
        f"GROUP BY org ORDER BY c DESC",
        *params,
    )
    clearances = await conn.fetch(
        f"SELECT clearance, COUNT(*) AS c FROM jobs_geo "
        f"WHERE clearance IS NOT NULL AND clearance != '' AND ({where}) "
        f"GROUP BY clearance ORDER BY c DESC",
        *params,
    )
    states = await conn.fetch(
        f"SELECT state, COUNT(*) AS c FROM jobs_geo "
        f"WHERE state IS NOT NULL AND state != '' AND ({where}) "
        f"GROUP BY state ORDER BY c DESC",
        *params,
    )
    remote_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM jobs_geo WHERE remote = true AND ({where})", *params
    )
    telework_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM jobs_geo WHERE telework = true AND ({where})", *params
    )
    countries = await conn.fetch(
        f"SELECT country, COUNT(*) AS c FROM jobs_geo "
        f"WHERE country IS NOT NULL AND country != '' AND ({where}) "
        f"GROUP BY country ORDER BY c DESC",
        *params,
    )
    cities = await conn.fetch(
        f"SELECT location_name, state, country, "
        f"AVG(ST_X(geom))::numeric(10,4) AS lon, "
        f"AVG(ST_Y(geom))::numeric(10,4) AS lat, "
        f"COUNT(*) AS c "
        f"FROM jobs_geo WHERE location_name IS NOT NULL AND ({where}) "
        f"GROUP BY location_name, state, country ORDER BY location_name",
        *params,
    )
    localities = await conn.fetch(
        f"SELECT locality_area, COUNT(*) AS c FROM jobs_geo "
        f"WHERE locality_area IS NOT NULL AND ({where}) "
        f"GROUP BY locality_area ORDER BY c DESC",
        *params,
    )
    series = await conn.fetch(
        f"SELECT series_code, series_name, COUNT(*) AS c FROM jobs_geo "
        f"WHERE series_code IS NOT NULL AND series_code != '' AND ({where}) "
        f"GROUP BY series_code, series_name ORDER BY c DESC",
        *params,
    )
    country_bounds = await conn.fetch(
        f"SELECT country, "
        f"MIN(ST_Y(geom))::numeric(10,4) AS south, "
        f"MIN(ST_X(geom))::numeric(10,4) AS west, "
        f"MAX(ST_Y(geom))::numeric(10,4) AS north, "
        f"MAX(ST_X(geom))::numeric(10,4) AS east "
        f"FROM jobs_geo WHERE country IS NOT NULL AND country != '' AND ({where}) "
        f"GROUP BY country",
        *params,
    )
    locality_bounds = await conn.fetch(
        f"SELECT locality_area, "
        f"MIN(ST_Y(geom))::numeric(10,4) AS south, "
        f"MIN(ST_X(geom))::numeric(10,4) AS west, "
        f"MAX(ST_Y(geom))::numeric(10,4) AS north, "
        f"MAX(ST_X(geom))::numeric(10,4) AS east "
        f"FROM jobs_geo WHERE locality_area IS NOT NULL AND ({where}) "
        f"GROUP BY locality_area",
        *params,
    )

    # Build department list with synthetic "Department of Defense (All)" entry
    dod_depts = set(DOD_DEPARTMENTS)
    dod_total = sum(r["c"] for r in departments if r["department"] in dod_depts)
    dept_list = []
    if dod_total > 0:
        dept_list.append({"name": "Department of Defense (All)", "count": dod_total})
    dept_list += [{"name": r["department"], "count": r["c"]} for r in departments]

    return {
        "departments": dept_list,
        "agencies": [{"name": r["org"], "count": r["c"]} for r in agencies],
        "clearances": [{"name": r["clearance"], "count": r["c"]} for r in clearances],
        "states": [
            *([{"name": "Remote", "count": remote_count}] if remote_count else []),
            *(
                [{"name": "Telework Eligible", "count": telework_count}]
                if telework_count
                else []
            ),
            *[{"name": r["state"], "count": r["c"]} for r in states],
        ],
        "countries": [{"name": r["country"], "count": r["c"]} for r in countries],
        "cities": [
            {
                "name": r["location_name"],
                "state": r["state"],
                "country": r["country"],
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "count": r["c"],
            }
            for r in cities
        ],
        "localities": [
            {"name": r["locality_area"], "count": r["c"]} for r in localities
        ],
        "series": [
            {
                "code": r["series_code"],
                "name": f"{r['series_code']} - {r['series_name']}",
                "count": r["c"],
            }
            for r in series
        ],
        "country_bounds": {
            r["country"]: [
                float(r["south"]),
                float(r["west"]),
                float(r["north"]),
                float(r["east"]),
            ]
            for r in country_bounds
        },
        "locality_bounds": {
            r["locality_area"]: [
                float(r["south"]),
                float(r["west"]),
                float(r["north"]),
                float(r["east"]),
            ]
            for r in locality_bounds
        },
    }


async def _get_last_refresh(conn) -> float:
    """Get last materialized view refresh timestamp as epoch seconds."""
    row = await conn.fetchval(
        "SELECT EXTRACT(EPOCH FROM last_refresh) FROM refresh_log WHERE id = 1"
    )
    return float(row) if row else 0.0


@router.get("/api/filters")
async def get_filters(
    department: Annotated[str | None, Query()] = None,
    agency: Annotated[str | None, Query()] = None,
    clearance: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    country: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    grade_min: Annotated[int | None, Query(ge=1, le=15)] = None,
    grade_max: Annotated[int | None, Query(ge=1, le=15)] = None,
    salary_min: Annotated[int | None, Query(ge=0)] = None,
    salary_max: Annotated[int | None, Query(ge=0)] = None,
    series: Annotated[str | None, Query()] = None,
    locality: Annotated[str | None, Query()] = None,
    exclude_ncr: Annotated[bool, Query()] = False,
    exclude_registers: Annotated[bool, Query()] = False,
    exclude_providers: Annotated[bool, Query()] = False,
    bbox: Annotated[str | None, Query()] = None,
):
    global _cache, _cache_ts

    has_filters = (
        any(
            v is not None
            for v in [
                department,
                agency,
                clearance,
                state,
                country,
                city,
                keyword,
                grade_min,
                grade_max,
                salary_min,
                salary_max,
                series,
                locality,
            ]
        )
        or exclude_ncr
        or exclude_registers
        or exclude_providers
        or bbox
    )

    pool = get_pool()
    async with pool.acquire(timeout=5) as conn:
        if not has_filters:
            # Unfiltered — use cache
            if _cache is not None:
                last_refresh = await _get_last_refresh(conn)
                if last_refresh <= _cache_ts:
                    return _cache
            logger.info("Loading filter options from database")
            _cache = await _load_filters(conn)
            _cache_ts = time.time()
            return _cache

        # Filtered — dynamic query
        bbox_coords = None
        if bbox:
            try:
                parts = [float(x) for x in bbox.split(",")]
                bbox_coords = tuple(parts) if len(parts) == 4 else None
            except ValueError:
                bbox_coords = None
        where, params = _build_where(
            department=department,
            agency=agency,
            clearance=clearance,
            state=state,
            country=country,
            city=city,
            keyword=keyword,
            grade_min=grade_min,
            grade_max=grade_max,
            salary_min=salary_min,
            salary_max=salary_max,
            series=series,
            locality=locality,
            exclude_ncr=exclude_ncr,
            exclude_registers=exclude_registers,
            exclude_providers=exclude_providers,
            bbox=bbox_coords,
        )
        return await _load_filters(conn, where, params)
