"""Commercial jobs API — ClearanceJobs postings from the private `commercial` schema.

List-only in P1: sitemap presence (consecutive_misses = 0) is the activity signal,
since CJ's validThrough is unreliable (evergreen 2016 postings still list).
"""

import json
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.api.filters import _parse_bbox
from backend.db import get_pool

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

LIMIT_DEFAULT = 25
LIMIT_MAX = 100
# Map endpoint returns raw points (job_locations has no geometry column, and each
# point carries its own popup payload); cap the page like the federal endpoint's
# individual path rather than clustering.
MAP_LIMIT = 2000

_SALARY_MIN = "data->'baseSalary'->'value'->>'minValue'"
_SALARY_MAX = "data->'baseSalary'->'value'->>'maxValue'"
_SALARY_TEXT = f"COALESCE({_SALARY_MIN}, {_SALARY_MAX})"
_NUMERIC = r"^[0-9]+(\.[0-9]+)?$"

_SORT_ORDERS = ("asc", "desc")

_ACTIVE = "source = 'clearancejobs' AND data IS NOT NULL AND consecutive_misses = 0"

# jobLocation is usually a list of Places but can be a single Place object;
# normalize both to a jsonb array so jsonb_array_elements can unnest it.
_JOB_LOCATIONS = (
    "CASE WHEN jsonb_typeof(data->'jobLocation') = 'array' "
    "THEN data->'jobLocation' "
    "ELSE jsonb_build_array(data->'jobLocation') END"
)


def _label_expr(t: str) -> str:
    """Combined display label over a commercial.job_locations alias `t`.

    US rows render "City, RG" (region code), everything else "City, Country" —
    the normalized-location convention the collector writes. `t` is always a
    server-side alias literal ("lo"), never user input.
    """
    return (
        f"CASE WHEN {t}.country = 'United States' "
        f"THEN {t}.city || ', ' || {t}.region "
        f"ELSE {t}.city || ', ' || {t}.country END"
    )


# Practical "DC commute" definition for exclude_ncr: the National Capital Region
# proper plus the Fort Meade corridor (BW Parkway / Anne Arundel). Lowercased to
# match against lower(addressLocality). VA/MD only — DC itself is caught by region.
# Tunable: add/remove suburbs here without touching the query.
NCR_CITIES = [
    # Northern Virginia
    "arlington",
    "alexandria",
    "falls church",
    "fairfax",
    "mclean",
    "tysons",
    "tysons corner",
    "vienna",
    "oakton",
    "merrifield",
    "reston",
    "herndon",
    "chantilly",
    "centreville",
    "springfield",
    "annandale",
    "burke",
    "ashburn",
    "sterling",
    "dulles",
    "leesburg",
    "manassas",
    "manassas park",
    "gainesville",
    "haymarket",
    "woodbridge",
    "lorton",
    "dumfries",
    "fort belvoir",
    "ft. belvoir",
    "quantico",
    "stafford",
    # Suburban Maryland + Fort Meade corridor
    "bethesda",
    "chevy chase",
    "rockville",
    "gaithersburg",
    "germantown",
    "silver spring",
    "wheaton",
    "college park",
    "greenbelt",
    "beltsville",
    "adelphi",
    "laurel",
    "columbia",
    "fort meade",
    "ft. meade",
    "ft meade",
    "annapolis junction",
    "hanover",
    "jessup",
    "odenton",
    "severn",
    "linthicum",
    "linthicum heights",
    "elkridge",
    "lanham",
    "hyattsville",
    "landover",
    "largo",
    "upper marlboro",
    "bowie",
    "suitland",
    "camp springs",
    "andrews afb",
    "joint base andrews",
]


def _sort_exprs(d: str, order: str = "desc") -> dict[str, str]:
    """Whitelisted ORDER BY expressions over the JobPosting jsonb at column `d`.

    `d` is always a server-side literal ("data" for the inner page CTE, "j.data"
    for the outer join) — never user input. Salary sorts numerically via the same
    guarded CASE the salary_min filter uses; absent/non-numeric values fall to NULL.
    Salary is direction-aware: highest-first ranks ranges by their ceiling
    (max, falling back to min), lowest-first by their floor (min, then max).
    """
    hi, lo = (
        f"{d}->'baseSalary'->'value'->>'maxValue'",
        f"{d}->'baseSalary'->'value'->>'minValue'",
    )
    salary = f"COALESCE({hi}, {lo})" if order == "desc" else f"COALESCE({lo}, {hi})"
    return {
        "posted": f"{d}->>'datePosted'",
        "close": f"{d}->>'validThrough'",
        "salary": f"CASE WHEN {salary} ~ '{_NUMERIC}' THEN ({salary})::numeric END",
        "title": f"{d}->>'title'",
        "company": f"{d}->'hiringOrganization'->>'name'",
        "clearance": f"{d}->>'securityClearanceRequirement'",
        "location": (
            f"COALESCE({d}->'jobLocation'->0->'address'->>'addressLocality', "
            f"{d}->'jobLocation'->'address'->>'addressLocality')"
        ),
    }


def _build_where(
    clearance=None,
    company=None,
    country=None,
    industry=None,
    employment_type=None,
    q=None,
    salary_min=None,
    location=None,
    loc=None,
    exclude_ncr=False,
) -> tuple[str, list]:
    """WHERE clause + $N-bound params for active CJ postings with detail data.

    Base predicate keeps id-only sightings and missed-sweep rows out: a row is
    surfaced only once its detail page is fetched (data IS NOT NULL) and it is
    still present in the latest sitemap sweep (consecutive_misses = 0).

    clearance/country/industry/employment_type are multi-value: each takes a list
    matched with `= ANY($N)` (one text[] param), values coming verbatim from the
    facet endpoint so exact matching is correct. country matches over the
    normalized jobLocation array, so a job on any listed country surfaces.

    loc is the combined-label facet (values verbatim from the locations facet): a
    job surfaces when any of its geocoded job_locations rows renders one of the
    given labels. `location` (free-text substring) stays independent.
    """
    clauses = [_ACTIVE]
    params: list = []

    if clearance:
        params.append(clearance)
        clauses.append(f"data->>'securityClearanceRequirement' = ANY(${len(params)})")
    if company:
        params.append(f"%{company}%")
        clauses.append(f"data->'hiringOrganization'->>'name' ILIKE ${len(params)}")
    if country:
        params.append(country)
        clauses.append(
            f"EXISTS (SELECT 1 FROM jsonb_array_elements({_JOB_LOCATIONS}) AS loc "
            f"WHERE loc->'address'->>'addressCountry' = ANY(${len(params)}))"
        )
    if industry:
        params.append(industry)
        clauses.append(f"data->>'industry' = ANY(${len(params)})")
    if employment_type:
        params.append(employment_type)
        clauses.append(f"data->>'employmentType' = ANY(${len(params)})")
    if q:
        params.append(f"%{q}%")
        n = len(params)
        clauses.append(f"(data->>'title' ILIKE ${n} OR slug ILIKE ${n})")
    if salary_min is not None:
        params.append(salary_min)
        clauses.append(
            f"CASE WHEN {_SALARY_TEXT} ~ '{_NUMERIC}' "
            f"THEN ({_SALARY_TEXT})::numeric END >= ${len(params)}"
        )
    if location:
        params.append(f"%{location}%")
        n = len(params)
        clauses.append(
            f"EXISTS (SELECT 1 FROM jsonb_array_elements({_JOB_LOCATIONS}) AS loc "
            f"WHERE loc->'address'->>'addressLocality' ILIKE ${n} "
            f"OR loc->'address'->>'addressRegion' ILIKE ${n})"
        )
    if exclude_ncr:
        params.append(NCR_CITIES)
        n = len(params)
        # Keep a job unless *every* location is NCR: EXISTS a location that is NOT
        # NCR. The COALESCEs are load-bearing — a location with null region/city
        # coalesces to '' (non-NCR), so it satisfies the NOT and keeps the job
        # rather than dropping out of the predicate via NULL. A missing jobLocation
        # normalizes to [null] (see _JOB_LOCATIONS): its lone element's
        # loc->'address' is NULL → coalesced to '' → non-NCR → job kept.
        clauses.append(
            f"EXISTS (SELECT 1 FROM jsonb_array_elements({_JOB_LOCATIONS}) AS loc "
            "WHERE NOT ("
            "COALESCE(loc->'address'->>'addressRegion', '') = 'DC' "
            "OR (COALESCE(loc->'address'->>'addressRegion', '') IN ('VA', 'MD') "
            f"AND lower(COALESCE(loc->'address'->>'addressLocality', '')) = ANY(${n})"
            ")))"
        )
    if loc:
        params.append(loc)
        clauses.append(
            "EXISTS (SELECT 1 FROM commercial.job_locations lo "
            "WHERE lo.source = jobs_raw.source AND lo.ext_id = jobs_raw.ext_id "
            f"AND {_label_expr('lo')} = ANY(${len(params)}))"
        )

    return " AND ".join(clauses), params


def _num(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _locations(job_location) -> tuple[list[str], list[str]]:
    """Flatten jobLocation (list or single Place) to "City, Region" + country lists."""
    if not job_location:
        return [], []
    places = json.loads(job_location)
    if isinstance(places, dict):
        places = [places]
    locs: list[str] = []
    countries: list[str] = []
    for place in places:
        addr = place.get("address") or {}
        if label := ", ".join(
            p for p in (addr.get("addressLocality"), addr.get("addressRegion")) if p
        ):
            locs.append(label)
        if (c := addr.get("addressCountry")) and c not in countries:
            countries.append(c)
    return locs, countries


def _item(row) -> dict:
    locations, countries = _locations(row["job_location"])
    return {
        "ext_id": row["ext_id"],
        "url": row["url"],
        "title": row["title"],
        "company": row["company"],
        "clearance": row["clearance"],
        "locations": locations,
        "country": countries,
        "employment_type": row["employment_type"],
        "date_posted": row["date_posted"],
        "valid_through": row["valid_through"],
        "industry": row["industry"],
        "salary_min": _num(row["salary_min"]),
        "salary_max": _num(row["salary_max"]),
    }


def _map_feature(row) -> dict:
    """GeoJSON point feature for the map, mirroring the federal endpoint's shape."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [float(row["lon"]), float(row["lat"])],
        },
        "properties": {
            "ext_id": row["ext_id"],
            "url": row["url"],
            "title": row["title"],
            "company": row["company"],
            "clearance": row["clearance"] or "",
            "salary_min": _num(row["salary_min"]),
            "salary_max": _num(row["salary_max"]),
            "location": row["label"],
        },
    }


@router.get("/api/commercial/jobs")
@limiter.limit("120/minute")
async def get_commercial_jobs(
    request: Request,
    clearance: Annotated[list[str] | None, Query()] = None,
    company: Annotated[str | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    industry: Annotated[list[str] | None, Query()] = None,
    employment_type: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    salary_min: Annotated[int | None, Query(ge=0)] = None,
    location: Annotated[str | None, Query()] = None,
    loc: Annotated[list[str] | None, Query()] = None,
    exclude_ncr: Annotated[bool, Query()] = False,
    sort: Annotated[str, Query()] = "posted",
    order: Annotated[str, Query()] = "desc",
    limit: Annotated[int, Query(ge=1, le=LIMIT_MAX)] = LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    inner_exprs = _sort_exprs("data", order)
    if sort not in inner_exprs or order not in _SORT_ORDERS:
        return Response(
            content=json.dumps({"error": "invalid sort or order", "code": 422}),
            status_code=422,
            media_type="application/json",
        )
    direction = "DESC" if order == "desc" else "ASC"
    inner_order = f"{inner_exprs[sort]} {direction} NULLS LAST, ext_id"
    outer_order = (
        f"{_sort_exprs('j.data', order)[sort]} {direction} NULLS LAST, j.ext_id"
    )

    where, params = _build_where(
        clearance=clearance,
        company=company,
        country=country,
        industry=industry,
        employment_type=employment_type,
        q=q,
        salary_min=salary_min,
        location=location,
        loc=loc,
        exclude_ncr=exclude_ncr,
    )

    pool = get_pool()
    async with pool.acquire(timeout=5) as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM commercial.jobs_raw WHERE {where}", *params
        )
        rows = await conn.fetch(
            f"""
            WITH page AS (
                SELECT source, ext_id
                FROM commercial.jobs_raw
                WHERE {where}
                ORDER BY {inner_order}
                LIMIT {limit} OFFSET {offset}
            )
            SELECT
                j.ext_id, j.url,
                j.data->>'title' AS title,
                j.data->'hiringOrganization'->>'name' AS company,
                j.data->>'securityClearanceRequirement' AS clearance,
                j.data->>'employmentType' AS employment_type,
                j.data->>'datePosted' AS date_posted,
                j.data->>'validThrough' AS valid_through,
                j.data->>'industry' AS industry,
                j.data->'jobLocation' AS job_location,
                j.{_SALARY_MIN} AS salary_min,
                j.{_SALARY_MAX} AS salary_max
            FROM page
            JOIN commercial.jobs_raw j USING (source, ext_id)
            ORDER BY {outer_order}
            """,
            *params,
        )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "jobs": [_item(r) for r in rows],
    }


@router.get("/api/commercial/map")
@limiter.limit("120/minute")
async def get_commercial_map(
    request: Request,
    bbox: Annotated[str, Query(description="west,south,east,north")],
    zoom: Annotated[int, Query(ge=0, le=18)] = 0,
    clearance: Annotated[list[str] | None, Query()] = None,
    company: Annotated[str | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    industry: Annotated[list[str] | None, Query()] = None,
    employment_type: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    salary_min: Annotated[int | None, Query(ge=0)] = None,
    location: Annotated[str | None, Query()] = None,
    loc: Annotated[list[str] | None, Query()] = None,
    exclude_ncr: Annotated[bool, Query()] = False,
):
    """Map points for the same active+filtered CJ set as /api/commercial/jobs.

    Takes every /api/commercial/jobs filter plus a bbox, and returns geocoded
    job_locations rows (lat/lon NOT NULL) inside it as a GeoJSON FeatureCollection
    shaped like the federal /api/jobs response. Raw points, not clusters: each
    carries its own popup payload and job_locations has no geometry to grid on.
    """
    try:
        west, south, east, north = _parse_bbox(bbox)
    except ValueError as e:
        return Response(
            content=json.dumps({"error": str(e), "code": 422}),
            status_code=422,
            media_type="application/json",
        )

    where, params = _build_where(
        clearance=clearance,
        company=company,
        country=country,
        industry=industry,
        employment_type=employment_type,
        q=q,
        salary_min=salary_min,
        location=location,
        loc=loc,
        exclude_ncr=exclude_ncr,
    )
    n = len(params)
    params.extend([west, south, east, north])

    # The matched CTE runs _build_where over commercial.jobs_raw alone, byte-for-byte
    # the same context as the list endpoint (so the loc EXISTS's jobs_raw.source
    # correlation resolves the same way); the join to job_locations then unnests one
    # point per geocoded location. bbox params bind after the content filters.
    sql = f"""
        WITH matched AS (
            SELECT source, ext_id, url, data
            FROM commercial.jobs_raw
            WHERE {where}
        )
        SELECT
            m.ext_id, m.url,
            m.data->>'title' AS title,
            m.data->'hiringOrganization'->>'name' AS company,
            m.data->>'securityClearanceRequirement' AS clearance,
            m.{_SALARY_MIN} AS salary_min,
            m.{_SALARY_MAX} AS salary_max,
            {_label_expr("lo")} AS label,
            lo.lat AS lat, lo.lon AS lon
        FROM matched m
        JOIN commercial.job_locations lo
          ON lo.source = m.source AND lo.ext_id = m.ext_id
        WHERE lo.lat IS NOT NULL AND lo.lon IS NOT NULL
          AND lo.lon BETWEEN ${n + 1} AND ${n + 3}
          AND lo.lat BETWEEN ${n + 2} AND ${n + 4}
        LIMIT {MAP_LIMIT}
    """

    pool = get_pool()
    async with pool.acquire(timeout=5) as conn:
        rows = await conn.fetch(sql, *params)

    features = [_map_feature(r) for r in rows]
    return {
        "type": "FeatureCollection",
        "metadata": {"total": len(features), "clustered": False, "zoom": zoom},
        "features": features,
    }


@router.get("/api/commercial/filters")
@limiter.limit("120/minute")
async def get_commercial_filters(request: Request):
    """Facet options (clearance, country, industry, employment type, location)
    over the active CJ posting set.

    Country and location counts are per-job: a posting with several locations
    sharing one value counts once, matching how those filters surface a job on
    any match.
    """
    pool = get_pool()
    async with pool.acquire(timeout=5) as conn:
        clearances = await conn.fetch(
            "SELECT data->>'securityClearanceRequirement' AS value, COUNT(*) AS c "
            f"FROM commercial.jobs_raw WHERE {_ACTIVE} "
            "AND data->>'securityClearanceRequirement' IS NOT NULL "
            "GROUP BY value ORDER BY c DESC, value"
        )
        countries = await conn.fetch(
            "SELECT value, COUNT(*) AS c FROM ("
            "SELECT DISTINCT ext_id, loc->'address'->>'addressCountry' AS value "
            f"FROM commercial.jobs_raw, jsonb_array_elements({_JOB_LOCATIONS}) AS loc "
            f"WHERE {_ACTIVE}) sub "
            "WHERE value IS NOT NULL GROUP BY value ORDER BY c DESC, value"
        )
        industries = await conn.fetch(
            "SELECT data->>'industry' AS value, COUNT(*) AS c "
            f"FROM commercial.jobs_raw WHERE {_ACTIVE} "
            "AND data->>'industry' IS NOT NULL "
            "GROUP BY value ORDER BY c DESC, value"
        )
        employment_types = await conn.fetch(
            "SELECT data->>'employmentType' AS value, COUNT(*) AS c "
            f"FROM commercial.jobs_raw WHERE {_ACTIVE} "
            "AND data->>'employmentType' IS NOT NULL "
            "GROUP BY value ORDER BY c DESC, value"
        )
        # Combined-label facet over geocoded locations of active postings. Counts
        # are per-job (DISTINCT ext_id): a posting with several rows sharing one
        # label counts once. No cap — the sidebar group scrolls.
        locations = await conn.fetch(
            "SELECT value, COUNT(DISTINCT ext_id) AS c FROM ("
            f"SELECT lo.ext_id AS ext_id, {_label_expr('lo')} AS value "
            "FROM commercial.job_locations lo "
            "WHERE lo.city IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM commercial.jobs_raw "
            f"WHERE jobs_raw.source = lo.source AND jobs_raw.ext_id = lo.ext_id "
            f"AND {_ACTIVE})) sub "
            "WHERE value IS NOT NULL GROUP BY value ORDER BY c DESC, value"
        )
    return {
        "clearances": [{"value": r["value"], "count": r["c"]} for r in clearances],
        "countries": [{"value": r["value"], "count": r["c"]} for r in countries],
        "industries": [{"value": r["value"], "count": r["c"]} for r in industries],
        "employment_types": [
            {"value": r["value"], "count": r["c"]} for r in employment_types
        ],
        "locations": [{"value": r["value"], "count": r["c"]} for r in locations],
    }
