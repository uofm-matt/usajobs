"""Commercial jobs API — ClearanceJobs postings from the private `commercial` schema.

List-only in P1: sitemap presence (consecutive_misses = 0) is the activity signal,
since CJ's validThrough is unreliable (evergreen 2016 postings still list).
"""

import json
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.db import get_pool

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

LIMIT_DEFAULT = 25
LIMIT_MAX = 100

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


def _sort_exprs(d: str) -> dict[str, str]:
    """Whitelisted ORDER BY expressions over the JobPosting jsonb at column `d`.

    `d` is always a server-side literal ("data" for the inner page CTE, "j.data"
    for the outer join) — never user input. Salary sorts numerically via the same
    guarded CASE the salary_min filter uses; absent/non-numeric values fall to NULL.
    """
    salary = f"COALESCE({d}->'baseSalary'->'value'->>'minValue', {d}->'baseSalary'->'value'->>'maxValue')"
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
    q=None,
    salary_min=None,
    location=None,
    exclude_ncr=False,
) -> tuple[str, list]:
    """WHERE clause + $N-bound params for active CJ postings with detail data.

    Base predicate keeps id-only sightings and missed-sweep rows out: a row is
    surfaced only once its detail page is fetched (data IS NOT NULL) and it is
    still present in the latest sitemap sweep (consecutive_misses = 0).
    """
    clauses = [_ACTIVE]
    params: list = []

    if clearance:
        params.append(clearance)
        clauses.append(f"data->>'securityClearanceRequirement' ILIKE ${len(params)}")
    if company:
        params.append(f"%{company}%")
        clauses.append(f"data->'hiringOrganization'->>'name' ILIKE ${len(params)}")
    if country:
        params.append(country)
        clauses.append(
            "jsonb_path_exists(data, "
            "'$.jobLocation[*].address.addressCountry ? (@ == $c)', "
            f"jsonb_build_object('c', ${len(params)}::text))"
        )
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


@router.get("/api/commercial/jobs")
@limiter.limit("120/minute")
async def get_commercial_jobs(
    request: Request,
    clearance: Annotated[str | None, Query()] = None,
    company: Annotated[str | None, Query()] = None,
    country: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    salary_min: Annotated[int | None, Query(ge=0)] = None,
    location: Annotated[str | None, Query()] = None,
    exclude_ncr: Annotated[bool, Query()] = False,
    sort: Annotated[str, Query()] = "posted",
    order: Annotated[str, Query()] = "desc",
    limit: Annotated[int, Query(ge=1, le=LIMIT_MAX)] = LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    inner_exprs = _sort_exprs("data")
    if sort not in inner_exprs or order not in _SORT_ORDERS:
        return Response(
            content=json.dumps({"error": "invalid sort or order", "code": 422}),
            status_code=422,
            media_type="application/json",
        )
    direction = "DESC" if order == "desc" else "ASC"
    inner_order = f"{inner_exprs[sort]} {direction} NULLS LAST, ext_id"
    outer_order = f"{_sort_exprs('j.data')[sort]} {direction} NULLS LAST, j.ext_id"

    where, params = _build_where(
        clearance=clearance,
        company=company,
        country=country,
        q=q,
        salary_min=salary_min,
        location=location,
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


@router.get("/api/commercial/filters")
@limiter.limit("120/minute")
async def get_commercial_filters(request: Request):
    """Facet options (clearance, country) over the active CJ posting set.

    Country counts are per-job: a posting with several locations in one country
    counts once, matching how the country filter surfaces a job on any match.
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
    return {
        "clearances": [{"value": r["value"], "count": r["c"]} for r in clearances],
        "countries": [{"value": r["value"], "count": r["c"]} for r in countries],
    }
