"""Jobs API endpoint — returns GeoJSON for map viewport with server-side clustering."""

import json
import math
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.api.filters import _parse_bbox, build_content_where
from backend.api.filters import DOD_DEPARTMENTS as DOD_DEPARTMENTS
from backend.api.filters import NCR_LOCALITY as NCR_LOCALITY
from backend.db import get_pool

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

RATE_LABELS = {
    "PA": "",
    "PH": "/hr",
    "PD": "/day",
    "PW": "/wk",
    "BW": "/2wk",
    "WC": "",
    "SY": "",
    "FB": "",
}
MAX_INDIVIDUAL = 2000
MAX_CLUSTERS = 150


# Deliberately ordered differently from filters._WHERE_ORDER: each endpoint's
# historical clause order fixes its $N param numbering, so the two lists must
# stay separate to keep emitted SQL byte-identical.
_CONTENT_ORDER = (
    "department",
    "agency",
    "grade_min",
    "grade_max",
    "salary_min",
    "salary_max",
    "clearance",
    "keyword",
    "state",
    "country",
    "city",
    "series",
    "locality",
    "exclude_ncr",
    "exclude_registers",
    "exclude_providers",
)


def _build_content_filters(
    clauses,
    params,
    agency=None,
    department=None,
    grade_min=None,
    grade_max=None,
    salary_min=None,
    salary_max=None,
    clearance=None,
    keyword=None,
    state=None,
    country=None,
    city=None,
    series=None,
    locality=None,
    exclude_ncr=False,
    exclude_registers=False,
    exclude_providers=False,
) -> tuple[str, list]:
    """Append content filter clauses (shared by spatial and list queries)."""
    values = {
        "department": department,
        "agency": agency,
        "grade_min": grade_min,
        "grade_max": grade_max,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "clearance": clearance,
        "keyword": keyword,
        "state": state,
        "country": country,
        "city": city,
        "series": series,
        "locality": locality,
        "exclude_ncr": exclude_ncr,
        "exclude_registers": exclude_registers,
        "exclude_providers": exclude_providers,
    }
    return build_content_where(_CONTENT_ORDER, values, clauses, params)


def _build_filters(
    west,
    south,
    east,
    north,
    agency=None,
    department=None,
    grade_min=None,
    grade_max=None,
    salary_min=None,
    salary_max=None,
    clearance=None,
    keyword=None,
    state=None,
    country=None,
    city=None,
    series=None,
    locality=None,
    exclude_ncr=False,
    exclude_registers=False,
    exclude_providers=False,
) -> tuple[str, list]:
    """Build parameterized WHERE clause with bbox and content filters."""
    clauses = ["geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)"]
    params: list = [west, south, east, north]
    return _build_content_filters(
        clauses,
        params,
        agency=agency,
        department=department,
        grade_min=grade_min,
        grade_max=grade_max,
        salary_min=salary_min,
        salary_max=salary_max,
        clearance=clearance,
        keyword=keyword,
        state=state,
        country=country,
        city=city,
        series=series,
        locality=locality,
        exclude_ncr=exclude_ncr,
        exclude_registers=exclude_registers,
        exclude_providers=exclude_providers,
    )


def _build_list_filters(
    bbox=None,
    agency=None,
    department=None,
    grade_min=None,
    grade_max=None,
    salary_min=None,
    salary_max=None,
    clearance=None,
    keyword=None,
    state=None,
    country=None,
    city=None,
    series=None,
    locality=None,
    exclude_ncr=False,
    exclude_registers=False,
    exclude_providers=False,
) -> tuple[str, list]:
    """Build parameterized WHERE clause for list view (optional map-viewport bbox)."""
    clauses: list[str] = []
    params: list = []
    if bbox is not None:
        west, south, east, north = bbox
        clauses.append("geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)")
        params.extend([west, south, east, north])
    return _build_content_filters(
        clauses,
        params,
        agency=agency,
        department=department,
        grade_min=grade_min,
        grade_max=grade_max,
        salary_min=salary_min,
        salary_max=salary_max,
        clearance=clearance,
        keyword=keyword,
        state=state,
        country=country,
        city=city,
        series=series,
        locality=locality,
        exclude_ncr=exclude_ncr,
        exclude_registers=exclude_registers,
        exclude_providers=exclude_providers,
    )


def _grid_size(zoom: int) -> float:
    """Compute ST_SnapToGrid cell size from zoom level."""
    return 360.0 / (2**zoom) / 4


async def _get_counts(conn, where: str, params: list) -> tuple[int, int]:
    """Fast total count and distinct-location count of matching features."""
    sql = f"""
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT ST_SnapToGrid(geom, 0.001)) AS locations
        FROM jobs_geo WHERE {where}
    """
    row = await conn.fetchrow(sql, *params)
    return row["total"], row["locations"]


def _format_salary(row) -> str:
    if not (row["min_salary"] and row["max_salary"]):
        return ""
    try:
        lo = int(float(row["min_salary"]))
        hi = int(float(row["max_salary"]))
        salary = f"${lo:,}\u2013${hi:,}"
        suffix = RATE_LABELS.get(row["rate_interval"] or "", "")
        return salary + suffix if suffix else salary
    except (ValueError, TypeError):
        return f"{row['min_salary']}\u2013{row['max_salary']}"


def _format_grade(row) -> str:
    if not (row["pay_plan"] and row["low_grade"]):
        return ""
    grade = f"{row['pay_plan']}-{row['low_grade']}"
    if row["high_grade"] and row["high_grade"] != row["low_grade"]:
        grade += f"/{row['high_grade']}"
    return grade


async def _fetch_individual(conn, where: str, params: list) -> list[dict]:
    """Fetch individual points as GeoJSON Feature dicts."""
    sql = f"""
        SELECT
            position_id, title, org, department,
            min_salary, max_salary, rate_interval,
            pay_plan, low_grade, high_grade,
            clearance, close_date,
            ST_X(geom) AS lon, ST_Y(geom) AS lat,
            location_name
        FROM jobs_geo
        WHERE {where}
        LIMIT {MAX_INDIVIDUAL}
    """
    rows = await conn.fetch(sql, *params)

    features = []
    for r in rows:
        salary = _format_salary(r)
        grade = _format_grade(r)

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(r["lon"]), float(r["lat"])],
                },
                "properties": {
                    "id": r["position_id"],
                    "title": r["title"],
                    "org": r["org"],
                    "department": r["department"],
                    "salary": salary,
                    "grade": grade,
                    "clearance": r["clearance"] or "",
                    "location": r["location_name"],
                    "close_date": r["close_date"] or "",
                },
            }
        )

    return features


async def _fetch_clusters(
    conn, where: str, params: list, zoom: int
) -> tuple[list, int]:
    """Fetch clustered features using ST_SnapToGrid. Returns (features, total_count).

    Dynamically increases grid_size if cluster count exceeds MAX_CLUSTERS.
    """
    grid = _grid_size(zoom)

    for _attempt in range(5):
        next_param = len(params) + 1
        sql = f"""
            WITH filtered AS (
                SELECT geom, org
                FROM jobs_geo
                WHERE {where}
            ),
            gridded AS (
                SELECT
                    ST_SnapToGrid(geom, ${next_param}::float) AS cell,
                    geom, org
                FROM filtered
            )
            SELECT
                ST_X(ST_Centroid(ST_Collect(geom))) AS lon,
                ST_Y(ST_Centroid(ST_Collect(geom))) AS lat,
                COUNT(*) AS point_count,
                MODE() WITHIN GROUP (ORDER BY org) AS top_agency,
                SUM(COUNT(*)) OVER () AS total
            FROM gridded
            GROUP BY cell
            ORDER BY point_count DESC
        """
        rows = await conn.fetch(sql, *params, grid)

        if len(rows) <= MAX_CLUSTERS:
            break

        # Too many clusters — double grid size and retry
        grid *= 2

    total = int(rows[0]["total"]) if rows else 0

    features = []
    for r in rows:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(r["lon"]), float(r["lat"])],
                },
                "properties": {
                    "cluster": True,
                    "point_count": int(r["point_count"]),
                    "top_agency": r["top_agency"] or "",
                },
            }
        )

    return features, total


@router.get("/api/jobs")
@limiter.limit("120/minute")
async def get_jobs(
    request: Request,
    bbox: Annotated[str, Query(description="west,south,east,north")],
    zoom: Annotated[int, Query(ge=0, le=18)],
    agency: Annotated[str | None, Query()] = None,
    department: Annotated[str | None, Query()] = None,
    grade_min: Annotated[int | None, Query(ge=1, le=15)] = None,
    grade_max: Annotated[int | None, Query(ge=1, le=15)] = None,
    salary_min: Annotated[int | None, Query(ge=0)] = None,
    salary_max: Annotated[int | None, Query(ge=0)] = None,
    clearance: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    country: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    series: Annotated[str | None, Query()] = None,
    locality: Annotated[str | None, Query()] = None,
    exclude_ncr: Annotated[bool, Query()] = False,
    exclude_registers: Annotated[bool, Query()] = False,
    exclude_providers: Annotated[bool, Query()] = False,
):
    try:
        west, south, east, north = _parse_bbox(bbox)
    except ValueError as e:
        return Response(
            content=json.dumps({"error": str(e), "code": 422}),
            status_code=422,
            media_type="application/json",
        )

    where, params = _build_filters(
        west,
        south,
        east,
        north,
        agency=agency,
        department=department,
        grade_min=grade_min,
        grade_max=grade_max,
        salary_min=salary_min,
        salary_max=salary_max,
        clearance=clearance,
        keyword=keyword,
        state=state,
        country=country,
        city=city,
        series=series,
        locality=locality,
        exclude_ncr=exclude_ncr,
        exclude_registers=exclude_registers,
        exclude_providers=exclude_providers,
    )

    pool = get_pool()
    async with pool.acquire(timeout=5) as conn:
        total, distinct_locs = await _get_counts(conn, where, params)

        # Cluster when:
        # - low zoom with moderate data
        # - too many points for browser
        # - many jobs stacked on same locations (but not at high zoom)
        # At zoom >= 12, only cluster if over the hard cap
        should_cluster = (
            (zoom < 8 and total > 50)
            or total > MAX_INDIVIDUAL
            or (zoom < 12 and total > 50 and distinct_locs < total * 0.6)
        )

        if should_cluster:
            features, total = await _fetch_clusters(conn, where, params, zoom)
            return {
                "type": "FeatureCollection",
                "metadata": {"total": total, "clustered": True, "zoom": zoom},
                "features": features,
            }
        else:
            features = await _fetch_individual(conn, where, params)
            return {
                "type": "FeatureCollection",
                "metadata": {"total": len(features), "clustered": False, "zoom": zoom},
                "features": features,
            }


SORT_COLUMNS = {
    "title": "title",
    "salary": "max_salary::numeric",
    "grade": "gs_max",
    "org": "org",
    "location": "location_name",
    "close_date": "close_date",
    "series": "series_code",
}


@router.get("/api/jobs/list")
@limiter.limit("120/minute")
async def get_jobs_list(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    sort: Annotated[str, Query()] = "close_date",
    order: Annotated[str, Query()] = "asc",
    bbox: Annotated[str | None, Query()] = None,
    agency: Annotated[str | None, Query()] = None,
    department: Annotated[str | None, Query()] = None,
    grade_min: Annotated[int | None, Query(ge=1, le=15)] = None,
    grade_max: Annotated[int | None, Query(ge=1, le=15)] = None,
    salary_min: Annotated[int | None, Query(ge=0)] = None,
    salary_max: Annotated[int | None, Query(ge=0)] = None,
    clearance: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    country: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    series: Annotated[str | None, Query()] = None,
    locality: Annotated[str | None, Query()] = None,
    exclude_ncr: Annotated[bool, Query()] = False,
    exclude_registers: Annotated[bool, Query()] = False,
    exclude_providers: Annotated[bool, Query()] = False,
):
    bbox_coords = None
    if bbox:
        try:
            bbox_coords = _parse_bbox(bbox)
        except ValueError:
            bbox_coords = None
    where, params = _build_list_filters(
        bbox=bbox_coords,
        agency=agency,
        department=department,
        grade_min=grade_min,
        grade_max=grade_max,
        salary_min=salary_min,
        salary_max=salary_max,
        clearance=clearance,
        keyword=keyword,
        state=state,
        country=country,
        city=city,
        series=series,
        locality=locality,
        exclude_ncr=exclude_ncr,
        exclude_registers=exclude_registers,
        exclude_providers=exclude_providers,
    )

    if not where:
        where = "TRUE"

    sort_col = SORT_COLUMNS.get(sort, "close_date")
    sort_dir = "DESC" if order == "desc" else "ASC"
    offset = (page - 1) * per_page

    pool = get_pool()
    async with pool.acquire(timeout=5) as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(DISTINCT position_id) FROM jobs_geo WHERE {where}", *params
        )

        sql = f"""
            SELECT DISTINCT ON (position_id)
                position_id, title, org, department,
                min_salary, max_salary, rate_interval,
                pay_plan, low_grade, high_grade, gs_min, gs_max,
                clearance, close_date, location_name, remote, telework,
                series_code, series_name
            FROM jobs_geo
            WHERE {where}
            ORDER BY position_id, location_name
        """
        sql = f"""
            SELECT * FROM ({sql}) AS jobs
            ORDER BY {sort_col} {sort_dir} NULLS LAST, position_id
            LIMIT {per_page} OFFSET {offset}
        """
        rows = await conn.fetch(sql, *params)

    jobs = []
    for r in rows:
        jobs.append(
            {
                "id": r["position_id"],
                "title": r["title"],
                "org": r["org"],
                "department": r["department"],
                "salary": _format_salary(r),
                "grade": _format_grade(r),
                "clearance": r["clearance"] or "",
                "location": r["location_name"],
                "close_date": r["close_date"] or "",
                "remote": r["remote"],
                "telework": r["telework"],
                "series": f"{r['series_code']} - {r['series_name']}"
                if r["series_code"]
                else "",
            }
        )

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page) if total else 0,
        "jobs": jobs,
    }


@router.get("/api/jobs/{position_id:path}")
async def get_job_detail(position_id: str):
    pool = get_pool()
    async with pool.acquire(timeout=5) as conn:
        # Only serve jobs still surfaced by jobs_geo (open for application and
        # recently confirmed); a passed deadline or stale posting 404s like any
        # other missing job, so no interface exposes it by direct ID.
        row = await conn.fetchrow(
            "SELECT j.data FROM jobs_raw j "
            "WHERE j.position_id = $1 "
            "AND EXISTS (SELECT 1 FROM jobs_geo g WHERE g.position_id = j.position_id)",
            position_id,
        )
        if not row:
            return Response(
                content='{"error": "Job not found", "code": 404}',
                status_code=404,
                media_type="application/json",
            )

        raw = json.loads(row["data"])
        mod = raw.get("MatchedObjectDescriptor", {})
        details = mod.get("UserArea", {}).get("Details", {})

        # Salary
        salary = ""
        rem = mod.get("PositionRemuneration", [])
        if rem:
            r = rem[0]
            try:
                lo = int(float(r.get("MinimumRange", 0)))
                hi = int(float(r.get("MaximumRange", 0)))
                salary = f"${lo:,}\u2013${hi:,}"
                suffix = RATE_LABELS.get(r.get("RateIntervalCode", ""), "")
                if suffix:
                    salary += suffix
            except (ValueError, TypeError):
                pass

        # Grade
        grade = ""
        job_grade = mod.get("JobGrade", [])
        if job_grade:
            code = job_grade[0].get("Code", "")
            low = details.get("LowGrade", "")
            high = details.get("HighGrade", "")
            if code and low:
                grade = f"{code}-{low}"
                if high and high != low:
                    grade += f"/{high}"

        # Major duties — join list items
        duties = details.get("MajorDuties", [])
        duties_text = (
            "\n".join(duties) if isinstance(duties, list) else str(duties or "")
        )

        # Locations
        locations = [
            loc.get("LocationName", "") for loc in mod.get("PositionLocation", [])
        ]

        return {
            "id": position_id,
            "title": mod.get("PositionTitle", ""),
            "org": mod.get("OrganizationName", ""),
            "department": mod.get("DepartmentName", ""),
            "salary": salary,
            "grade": grade,
            "locations": locations,
            "summary": mod.get("QualificationSummary", ""),
            "duties": duties_text,
            "education": details.get("Education", ""),
            "requirements": details.get("Requirements", ""),
            "evaluations": details.get("Evaluations", ""),
            "how_to_apply": details.get("HowToApply", ""),
            "clearance": details.get("SecurityClearance", ""),
            "hiring_paths": details.get("HiringPath", []),
            "travel": details.get("TravelCode", ""),
            "telework": details.get("TeleworkEligible", ""),
            "promotion_potential": details.get("PromotionPotential", ""),
            "open_date": mod.get("PositionStartDate", ""),
            "close_date": mod.get("PositionEndDate", ""),
            "apply_url": (mod.get("ApplyURI") or [""])[0],
            "usajobs_url": mod.get("PositionURI", ""),
        }
