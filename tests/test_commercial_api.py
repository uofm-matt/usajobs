"""Tests for /api/commercial/jobs — fully mocked, no live DB, no network.

Unit tests cover the WHERE builder and item shaping; endpoint tests drive the
route through the ASGI app with the asyncpg pool replaced by an AsyncMock.
"""

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock

from backend.api.commercial import (
    _ACTIVE,
    NCR_LOCALITY,
    _build_where,
    _item,
    _label_expr,
    _locations,
    _num,
    _sort_exprs,
)
from backend.main import app

# The base predicate clauses, derived from _ACTIVE so the freshness cut and any
# future additions stay in sync (the pieces split cleanly on " AND ").
BASE = _ACTIVE.split(" AND ")

# Combined display label over a job_locations alias, as the loc filter, map query,
# and locations facet all emit it.
_LABEL = _label_expr("lo")


# --- WHERE builder ---


class TestBuildWhere:
    def test_base_predicate_only(self):
        where, params = _build_where()
        assert where == " AND ".join(BASE)
        assert params == []

    def test_every_param_has_placeholder(self):
        where, params = _build_where(
            clearance=["Secret"],
            company="Acme Defense",
            country=["United States"],
            industry=["Aerospace"],
            employment_type=["Full-time"],
            q="engineer",
            salary_min=90000,
        )
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_no_user_values_in_sql(self):
        where, _ = _build_where(
            clearance=["Secret"],
            company="Acme Defense",
            country=["United States"],
            industry=["Aerospace"],
            employment_type=["Full-time"],
            q="engineer",
            salary_min=90000,
        )
        for literal in (
            "Secret",
            "Acme Defense",
            "United States",
            "Aerospace",
            "Full-time",
            "engineer",
            "90000",
        ):
            assert literal not in where

    def test_clearance_multi_value_any(self):
        # Multi-value: exact match against a single text[] param via = ANY.
        where, params = _build_where(clearance=["Secret", "Top Secret"])
        assert "data->>'securityClearanceRequirement' = ANY($1)" in where
        assert params == [["Secret", "Top Secret"]]

    def test_clearance_single_element_list(self):
        where, params = _build_where(clearance=["Secret"])
        assert "data->>'securityClearanceRequirement' = ANY($1)" in where
        assert params == [["Secret"]]

    def test_company_wrapped_in_wildcards(self):
        where, params = _build_where(company="Valida")
        assert "data->'hiringOrganization'->>'name' ILIKE $1" in where
        assert params == ["%Valida%"]

    def test_country_multi_value_exists_over_normalized_locations(self):
        where, params = _build_where(country=["United States", "Germany"])
        # EXISTS over the normalized jobLocation array (list or single Place),
        # matching addressCountry against the text[] param with = ANY.
        assert "jsonb_typeof(data->'jobLocation') = 'array'" in where
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in where
        assert "loc->'address'->>'addressCountry' = ANY($1)" in where
        assert "jsonb_path_exists" not in where
        assert params == [["United States", "Germany"]]

    def test_industry_multi_value_any(self):
        where, params = _build_where(industry=["Information Technology", "Aerospace"])
        assert "data->>'industry' = ANY($1)" in where
        assert params == [["Information Technology", "Aerospace"]]

    def test_employment_type_multi_value_any(self):
        where, params = _build_where(employment_type=["Full-time", "Contract"])
        assert "data->>'employmentType' = ANY($1)" in where
        assert params == [["Full-time", "Contract"]]

    def test_four_facets_combine_with_coherent_numbering(self):
        where, params = _build_where(
            clearance=["Secret"],
            country=["United States"],
            industry=["Aerospace"],
            employment_type=["Full-time"],
        )
        assert "data->>'securityClearanceRequirement' = ANY($1)" in where
        assert "loc->'address'->>'addressCountry' = ANY($2)" in where
        assert "data->>'industry' = ANY($3)" in where
        assert "data->>'employmentType' = ANY($4)" in where
        assert params == [["Secret"], ["United States"], ["Aerospace"], ["Full-time"]]
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_keyword_matches_title_or_slug_with_one_param(self):
        where, params = _build_where(q="analyst")
        assert "(data->>'title' ILIKE $1 OR slug ILIKE $1)" in where
        assert params == ["%analyst%"]

    def test_salary_min_guards_cast_and_excludes_nulls(self):
        where, params = _build_where(salary_min=90000)
        # CASE guard means a non-numeric/absent salary yields NULL, which the
        # `>= $1` comparison drops — null-salary rows are excluded.
        assert "CASE WHEN" in where
        assert "baseSalary" in where
        assert "::numeric END >= $1" in where
        assert params == [90000]

    def test_salary_min_zero_is_applied(self):
        # 0 is falsy but a valid floor — `is not None` must let it through.
        _, params = _build_where(salary_min=0)
        assert params == [0]

    def test_placeholders_sequential_across_filters(self):
        where, params = _build_where(clearance=["Secret"], company="X", q="y")
        assert "= ANY($1)" in where  # clearance
        assert "$2" in where  # company
        assert "$3" in where  # keyword (title + slug)
        assert len(params) == 3

    def test_location_normalizes_both_joblocation_shapes(self):
        where, params = _build_where(location="Denver")
        # One CASE handles jobLocation as a list or a single Place object, so the
        # same clause matches whichever shape the row stores.
        assert "jsonb_typeof(data->'jobLocation') = 'array'" in where
        assert "THEN data->'jobLocation'" in where
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in where
        # Parameterized ILIKE against locality OR region — no like_regex on input.
        assert "loc->'address'->>'addressLocality' ILIKE $1" in where
        assert "loc->'address'->>'addressRegion' ILIKE $1" in where
        assert "like_regex" not in where
        assert params == ["%Denver%"]

    def test_location_expands_state_name_to_region_code(self):
        # "colorado" isn't a substring of the stored "CO"/"Denver", so a full
        # state name expands to an exact region match on its USPS code.
        where, params = _build_where(location="Colorado")
        assert "loc->'address'->>'addressLocality' ILIKE $1" in where
        assert "loc->'address'->>'addressRegion' ILIKE $1" in where
        assert "loc->'address'->>'addressRegion' = $2" in where
        assert params == ["%Colorado%", "CO"]

    def test_location_non_state_binds_no_extra_param(self):
        # A city name that isn't a state leaves the clause single-param.
        where, params = _build_where(location="Denver")
        assert "addressRegion' = $2" not in where
        assert params == ["%Denver%"]

    def test_location_placeholder_sequential_after_other_filters(self):
        where, params = _build_where(
            clearance=["Secret"], company="X", q="y", location="Denver"
        )
        assert "= ANY($1)" in where  # clearance
        assert "$2" in where  # company
        assert "$3" in where  # keyword (title + slug)
        assert "ILIKE $4" in where  # location locality + region
        assert params == [["Secret"], "%X%", "%y%", "%Denver%"]
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_exclude_ncr_off_by_default_leaves_sql_unchanged(self):
        # Default (unchecked) must not touch the base predicate or bind a param.
        where, params = _build_where()
        assert where == " AND ".join(BASE)
        assert params == []
        assert "= ANY(" not in where

    def test_base_predicate_cuts_stale_postings(self):
        # The freshness cut lives in the base predicate so list, map, and facets
        # all drop postings older than 6 months by default.
        where, _ = _build_where()
        assert "left(data->>'datePosted', 10) >=" in where
        assert "now() - interval '6 months'" in where

    def test_remote_matches_telecommute_flag_or_title(self):
        where, params = _build_where(remote=True)
        # Structured schema.org flag OR the unambiguous title words — never the bare
        # "remote" (which also means "remote sensing"). No bound param.
        assert "data->>'jobLocationType' = 'TELECOMMUTE'" in where
        assert "data->>'title' ILIKE '%telecommute%'" in where
        assert "data->>'title' ILIKE '%telework%'" in where
        assert "'%remote%'" not in where
        assert params == []

    def test_remote_off_by_default(self):
        where, _ = _build_where()
        assert "TELECOMMUTE" not in where

    def test_max_age_days_narrows_with_bound_param(self):
        where, params = _build_where(max_age_days=30)
        assert "make_interval(days => $1)" in where
        assert "left(data->>'datePosted', 10) >=" in where
        assert params == [30]

    def test_max_age_days_off_by_default(self):
        where, params = _build_where()
        assert "make_interval" not in where
        assert params == []

    def test_bbox_scopes_to_geocoded_locations(self):
        where, params = _build_where(bbox=(-105.0, 39.0, -104.0, 40.0))
        # Job-level viewport filter: EXISTS a geocoded location inside the box.
        assert "EXISTS (SELECT 1 FROM commercial.job_locations lo" in where
        assert "lo.source = jobs_raw.source AND lo.ext_id = jobs_raw.ext_id" in where
        assert "lo.lon BETWEEN $1 AND $3" in where
        assert "lo.lat BETWEEN $2 AND $4" in where
        assert params == [-105.0, 39.0, -104.0, 40.0]

    def test_bbox_off_by_default(self):
        where, params = _build_where()
        assert "lo.lon BETWEEN" not in where
        assert params == []

    def test_exclude_ncr_filters_on_materialized_locality(self):
        where, params = _build_where(exclude_ncr=True)
        # Keep a job unless EVERY geocoded location is in the DC locality area:
        # EXISTS a location whose materialized locality_area differs (NULL counts
        # as non-NCR via IS DISTINCT FROM), OR the job has no location rows at all.
        assert "commercial.job_locations lo " in where
        assert "lo.source = jobs_raw.source AND lo.ext_id = jobs_raw.ext_id" in where
        assert "lo.locality_area IS DISTINCT FROM $1" in where
        assert "NOT EXISTS (SELECT 1 FROM commercial.job_locations lo2" in where
        # The single bound param is the shared federal DC locality name.
        assert params == [NCR_LOCALITY]

    def test_exclude_ncr_placeholder_sequential_after_other_filters(self):
        where, params = _build_where(
            clearance=["Secret"],
            company="X",
            q="y",
            location="Denver",
            exclude_ncr=True,
        )
        assert "= ANY($1)" in where  # clearance
        assert "$2" in where  # company
        assert "$3" in where  # keyword (title + slug)
        assert "ILIKE $4" in where  # location locality + region
        assert "IS DISTINCT FROM $5" in where  # NCR locality
        assert params == [["Secret"], "%X%", "%y%", "%Denver%", NCR_LOCALITY]
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_loc_none_leaves_sql_unchanged(self):
        where, params = _build_where()
        assert "commercial.job_locations lo" not in where
        assert params == []

    def test_loc_multi_value_exists_over_job_locations(self):
        where, params = _build_where(loc=["Denver, CO", "Reston, VA"])
        # EXISTS over the per-posting geocoded locations, matching the combined
        # label against the text[] param with = ANY. One bound param (the list).
        assert "EXISTS (SELECT 1 FROM commercial.job_locations lo" in where
        assert "lo.source = jobs_raw.source AND lo.ext_id = jobs_raw.ext_id" in where
        assert "lo.city || ', ' || lo.region" in where
        assert "lo.city || ', ' || lo.country" in where
        assert "= ANY($1)" in where
        assert params == [["Denver, CO", "Reston, VA"]]

    def test_loc_placeholder_sequential_after_other_filters(self):
        # loc numbers last so it never shifts the existing filters' placeholders.
        where, params = _build_where(
            clearance=["Secret"],
            company="X",
            q="y",
            location="Denver",
            exclude_ncr=True,
            loc=["Denver, CO"],
        )
        assert "data->>'securityClearanceRequirement' = ANY($1)" in where  # clearance
        assert "$2" in where  # company
        assert "$3" in where  # keyword (title + slug)
        assert "ILIKE $4" in where  # free-text location locality + region
        assert "lo.locality_area IS DISTINCT FROM $5" in where  # NCR locality
        assert f"{_LABEL} = ANY($6)" in where  # loc combined-label list
        assert params == [
            ["Secret"],
            "%X%",
            "%y%",
            "%Denver%",
            NCR_LOCALITY,
            ["Denver, CO"],
        ]
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"


# --- Item shaping ---


class TestLocations:
    def test_list_shaped_job_location(self):
        raw = json.dumps(
            [
                {
                    "address": {
                        "addressLocality": "Washington",
                        "addressRegion": "DC",
                        "addressCountry": "United States",
                    }
                },
                {
                    "address": {
                        "addressLocality": "Reston",
                        "addressRegion": "VA",
                        "addressCountry": "United States",
                    }
                },
            ]
        )
        locs, countries = _locations(raw)
        assert locs == ["Washington, DC", "Reston, VA"]
        assert countries == ["United States"]  # deduped

    def test_single_object_job_location(self):
        raw = json.dumps(
            {
                "address": {
                    "addressLocality": "Denver",
                    "addressRegion": "CO",
                    "addressCountry": "United States",
                }
            }
        )
        locs, countries = _locations(raw)
        assert locs == ["Denver, CO"]
        assert countries == ["United States"]

    def test_none_and_empty(self):
        assert _locations(None) == ([], [])
        assert _locations(json.dumps([])) == ([], [])

    def test_partial_address_fields(self):
        raw = json.dumps([{"address": {"addressRegion": "VA"}}, {"address": {}}])
        locs, countries = _locations(raw)
        assert locs == ["VA"]
        assert countries == []


class TestNum:
    def test_numeric_string(self):
        assert _num("90000") == 90000
        assert _num("90000.50") == 90000

    def test_non_numeric_and_none(self):
        assert _num(None) is None
        assert _num("Negotiable") is None
        assert _num("") is None


def _row(**over):
    row = {
        "ext_id": "1000001",
        "url": "https://www.clearancejobs.com/jobs/1000001/systems-engineer",
        "title": "Systems Engineer",
        "company": "Acme Defense",
        "clearance": "Secret",
        "employment_type": "Full-time",
        "date_posted": "2020-05-04T14:48:58-05:00",
        "valid_through": "2020-08-03T19:48:58+00:00Z",
        "industry": "Information Technology",
        "job_location": json.dumps(
            [
                {
                    "address": {
                        "addressLocality": "Washington",
                        "addressRegion": "DC",
                        "addressCountry": "United States",
                    }
                }
            ]
        ),
        "salary_min": None,
        "salary_max": None,
    }
    row.update(over)
    return row


class TestItem:
    def test_full_shape(self):
        item = _item(_row(salary_min="90000", salary_max="120000"))
        assert item == {
            "ext_id": "1000001",
            "url": "https://www.clearancejobs.com/jobs/1000001/systems-engineer",
            "title": "Systems Engineer",
            "company": "Acme Defense",
            "clearance": "Secret",
            "locations": ["Washington, DC"],
            "country": ["United States"],
            "employment_type": "Full-time",
            "date_posted": "2020-05-04T14:48:58-05:00",
            "valid_through": "2020-08-03T19:48:58+00:00Z",
            "industry": "Information Technology",
            "salary_min": 90000,
            "salary_max": 120000,
        }

    def test_null_safe_salary(self):
        item = _item(_row())
        assert item["salary_min"] is None
        assert item["salary_max"] is None


# --- Endpoint (mocked pool) ---


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self, timeout=None):
        return _Acquire(self._conn)


@pytest_asyncio.fixture
async def client(monkeypatch):
    """ASGI client whose route talks to an AsyncMock conn instead of Postgres."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    conn.fetch.return_value = []
    monkeypatch.setattr("backend.api.commercial.get_pool", lambda: _Pool(conn))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, conn


class TestEndpoint:
    async def test_empty_result(self, client):
        ac, _ = client
        r = await ac.get("/api/commercial/jobs")
        assert r.status_code == 200
        body = r.json()
        assert body == {"total": 0, "limit": 25, "offset": 0, "jobs": []}

    async def test_item_shape_and_url_click_target(self, client):
        ac, conn = client
        conn.fetchval.return_value = 1
        conn.fetch.return_value = [_row(salary_min="90000", salary_max="120000")]
        r = await ac.get("/api/commercial/jobs")
        body = r.json()
        assert body["total"] == 1
        job = body["jobs"][0]
        assert job["url"].startswith("https://www.clearancejobs.com/jobs/")
        assert job["locations"] == ["Washington, DC"]
        assert job["country"] == ["United States"]
        assert job["salary_min"] == 90000

    async def test_pagination_params_and_sql(self, client):
        ac, conn = client
        conn.fetchval.return_value = 200
        r = await ac.get("/api/commercial/jobs", params={"limit": 5, "offset": 10})
        body = r.json()
        assert body["limit"] == 5
        assert body["offset"] == 10
        sql = conn.fetch.call_args.args[0]
        assert "LIMIT 5 OFFSET 10" in sql
        assert "commercial.jobs_raw" in sql
        assert "ORDER BY data->>'datePosted' DESC NULLS LAST, ext_id" in sql

    async def test_keyword_filter_binds_param(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"q": "engineer"})
        # Both count and list queries carry the same bound params.
        count_params = conn.fetchval.call_args.args[1:]
        list_params = conn.fetch.call_args.args[1:]
        assert count_params == ("%engineer%",)
        assert list_params == ("%engineer%",)
        assert "ILIKE $1 OR slug ILIKE $1" in conn.fetch.call_args.args[0]

    async def test_salary_filter_excludes_null_salary_in_sql(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"salary_min": 90000})
        sql = conn.fetch.call_args.args[0]
        assert "CASE WHEN" in sql and "baseSalary" in sql
        assert conn.fetch.call_args.args[1:] == (90000,)

    async def test_clearance_multi_value_binds_one_list_param(self, client):
        ac, conn = client
        await ac.get(
            "/api/commercial/jobs", params={"clearance": ["Secret", "Top Secret"]}
        )
        sql = conn.fetch.call_args.args[0]
        assert "data->>'securityClearanceRequirement' = ANY($1)" in sql
        # Repeated query params arrive as one text[] param on both queries.
        assert conn.fetchval.call_args.args[1:] == (["Secret", "Top Secret"],)
        assert conn.fetch.call_args.args[1:] == (["Secret", "Top Secret"],)

    async def test_clearance_single_element_list(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"clearance": "Secret"})
        assert "= ANY($1)" in conn.fetch.call_args.args[0]
        assert conn.fetch.call_args.args[1:] == (["Secret"],)

    async def test_country_multi_value_uses_exists_normalization(self, client):
        ac, conn = client
        await ac.get(
            "/api/commercial/jobs", params={"country": ["United States", "Germany"]}
        )
        sql = conn.fetch.call_args.args[0]
        assert "jsonb_path_exists" not in sql
        assert "jsonb_array_elements" in sql
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in sql
        assert "loc->'address'->>'addressCountry' = ANY($1)" in sql
        assert conn.fetch.call_args.args[1:] == (["United States", "Germany"],)

    async def test_industry_multi_value_binds_one_list_param(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"industry": ["Aerospace", "IT"]})
        assert "data->>'industry' = ANY($1)" in conn.fetch.call_args.args[0]
        assert conn.fetch.call_args.args[1:] == (["Aerospace", "IT"],)

    async def test_employment_type_multi_value_binds_one_list_param(self, client):
        ac, conn = client
        await ac.get(
            "/api/commercial/jobs",
            params={"employment_type": ["Full-time", "Contract"]},
        )
        assert "data->>'employmentType' = ANY($1)" in conn.fetch.call_args.args[0]
        assert conn.fetch.call_args.args[1:] == (["Full-time", "Contract"],)

    async def test_multiple_facet_categories_combine_coherent_numbering(self, client):
        ac, conn = client
        await ac.get(
            "/api/commercial/jobs",
            params={
                "clearance": "Secret",
                "country": "United States",
                "industry": "Aerospace",
                "employment_type": "Full-time",
            },
        )
        sql = conn.fetch.call_args.args[0]
        assert "data->>'securityClearanceRequirement' = ANY($1)" in sql
        assert "loc->'address'->>'addressCountry' = ANY($2)" in sql
        assert "data->>'industry' = ANY($3)" in sql
        assert "data->>'employmentType' = ANY($4)" in sql
        assert conn.fetch.call_args.args[1:] == (
            ["Secret"],
            ["United States"],
            ["Aerospace"],
            ["Full-time"],
        )

    async def test_location_filter_binds_wildcarded_param(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"location": "Denver"})
        sql = conn.fetch.call_args.args[0]
        assert "jsonb_array_elements" in sql
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in sql
        assert "ILIKE $1" in sql
        assert conn.fetch.call_args.args[1:] == ("%Denver%",)

    async def test_exclude_ncr_binds_locality_param(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"exclude_ncr": 1})
        sql = conn.fetch.call_args.args[0]
        assert "lo.locality_area IS DISTINCT FROM $1" in sql
        assert "NOT EXISTS (SELECT 1 FROM commercial.job_locations lo2" in sql
        # One param: the DC locality name, bound to both the count and list queries.
        assert conn.fetchval.call_args.args[1:] == (NCR_LOCALITY,)
        assert conn.fetch.call_args.args[1:] == (NCR_LOCALITY,)

    async def test_exclude_ncr_absent_leaves_no_ncr_clause(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs")
        sql = conn.fetch.call_args.args[0]
        assert "IN ('VA', 'MD')" not in sql
        assert conn.fetch.call_args.args[1:] == ()

    async def test_limit_over_cap_rejected(self, client):
        ac, _ = client
        r = await ac.get("/api/commercial/jobs", params={"limit": 101})
        assert r.status_code == 422


# --- Sorting ---

SORT_FIELDS = ["posted", "close", "salary", "title", "company", "clearance", "location"]


class TestSort:
    @pytest.mark.parametrize("field", SORT_FIELDS)
    @pytest.mark.parametrize("order,direction", [("asc", "ASC"), ("desc", "DESC")])
    async def test_expression_in_both_order_bys(self, client, field, order, direction):
        # The chosen expression must appear in the inner page CTE (unqualified) and
        # the outer join (j. qualified), with the direction, NULLS LAST, and the
        # ext_id tiebreaker in each.
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"sort": field, "order": order})
        sql = conn.fetch.call_args.args[0]
        exprs = _sort_exprs("data", order)
        inner = f"ORDER BY {exprs[field]} {direction} NULLS LAST, ext_id"
        outer = (
            f"ORDER BY {_sort_exprs('j.data', order)[field]} {direction}"
            " NULLS LAST, j.ext_id"
        )
        assert inner in sql
        assert outer in sql

    async def test_salary_sorts_numerically_not_text(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"sort": "salary"})
        sql = conn.fetch.call_args.args[0]
        assert "CASE WHEN" in sql
        assert "baseSalary" in sql
        assert "::numeric END DESC" in sql

    async def test_salary_direction_picks_range_bound(self, client):
        # Highest-first ranks by the range ceiling (max, then min); lowest-first
        # by the floor (min, then max).
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"sort": "salary"})
        assert (
            "COALESCE(data->'baseSalary'->'value'->>'maxValue', data->'baseSalary'->'value'->>'minValue')"
            in conn.fetch.call_args.args[0]
        )
        await ac.get("/api/commercial/jobs", params={"sort": "salary", "order": "asc"})
        assert (
            "COALESCE(data->'baseSalary'->'value'->>'minValue', data->'baseSalary'->'value'->>'maxValue')"
            in conn.fetch.call_args.args[0]
        )

    async def test_nulls_last_in_both_clauses_regardless_of_direction(self, client):
        ac, conn = client
        for order in ("asc", "desc"):
            await ac.get(
                "/api/commercial/jobs", params={"sort": "close", "order": order}
            )
            sql = conn.fetch.call_args.args[0]
            assert sql.count("NULLS LAST") == 2

    async def test_default_is_posted_desc(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs")
        sql = conn.fetch.call_args.args[0]
        assert "ORDER BY data->>'datePosted' DESC NULLS LAST, ext_id" in sql
        assert "ORDER BY j.data->>'datePosted' DESC NULLS LAST, j.ext_id" in sql

    async def test_invalid_sort_rejected(self, client):
        ac, _ = client
        r = await ac.get("/api/commercial/jobs", params={"sort": "bogus"})
        assert r.status_code == 422

    async def test_invalid_order_rejected(self, client):
        ac, _ = client
        r = await ac.get("/api/commercial/jobs", params={"order": "sideways"})
        assert r.status_code == 422

    async def test_invalid_sort_never_reaches_the_database(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"sort": "ext_id; DROP TABLE x"})
        conn.fetch.assert_not_called()


# --- Facet options ---

ACTIVE = "source = 'clearancejobs' AND data IS NOT NULL AND consecutive_misses = 0"


class TestFiltersEndpoint:
    async def test_documented_shape(self, client):
        ac, conn = client
        conn.fetch.side_effect = [
            [{"value": "Secret", "c": 12}, {"value": "Top Secret", "c": 3}],
            [{"value": "United States", "c": 40}, {"value": "Germany", "c": 2}],
            [{"value": "Aerospace", "c": 30}, {"value": "IT", "c": 9}],
            [{"value": "Full-time", "c": 50}, {"value": "Contract", "c": 4}],
            [{"value": "Denver, CO", "c": 15}, {"value": "Reston, VA", "c": 7}],
        ]
        r = await ac.get("/api/commercial/filters")
        assert r.status_code == 200
        assert r.json() == {
            "clearances": [
                {"value": "Secret", "count": 12},
                {"value": "Top Secret", "count": 3},
            ],
            "countries": [
                {"value": "United States", "count": 40},
                {"value": "Germany", "count": 2},
            ],
            "industries": [
                {"value": "Aerospace", "count": 30},
                {"value": "IT", "count": 9},
            ],
            "employment_types": [
                {"value": "Full-time", "count": 50},
                {"value": "Contract", "count": 4},
            ],
            "locations": [
                {"value": "Denver, CO", "count": 15},
                {"value": "Reston, VA", "count": 7},
            ],
        }

    async def test_sql_targets_active_predicate(self, client):
        ac, conn = client
        await ac.get("/api/commercial/filters")
        clearance_sql, country_sql, industry_sql, employment_sql, locations_sql = (
            c.args[0] for c in conn.fetch.call_args_list
        )
        for sql in (
            clearance_sql,
            country_sql,
            industry_sql,
            employment_sql,
            locations_sql,
        ):
            assert ACTIVE in sql
        assert "securityClearanceRequirement" in clearance_sql
        # Country facet normalizes list/single-object jobLocation like the filter.
        assert "addressCountry" in country_sql
        assert "jsonb_array_elements" in country_sql
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in country_sql
        assert "data->>'industry'" in industry_sql
        assert "data->>'employmentType'" in employment_sql

    async def test_locations_facet_sql(self, client):
        ac, conn = client
        await ac.get("/api/commercial/filters")
        locations_sql = conn.fetch.call_args_list[4].args[0]
        # Combined label over geocoded job_locations of active postings.
        assert "commercial.job_locations lo" in locations_sql
        assert "lo.city IS NOT NULL" in locations_sql
        assert "lo.city || ', ' || lo.region" in locations_sql
        assert "lo.city || ', ' || lo.country" in locations_sql
        # Per-job count (a posting counted once per label) and count DESC, value.
        assert "COUNT(DISTINCT ext_id)" in locations_sql
        assert "ORDER BY c DESC, value" in locations_sql


# --- Map endpoint (mocked pool) ---

BBOX = "-109,37,-102,41"
BBOX_COORDS = (-109.0, 37.0, -102.0, 41.0)


def _map_row(**over):
    row = {
        "ext_id": "1000001",
        "url": "https://www.clearancejobs.com/jobs/1000001/systems-engineer",
        "title": "Systems Engineer",
        "company": "Acme Defense",
        "clearance": "Secret",
        "salary_min": None,
        "salary_max": None,
        "label": "Denver, CO",
        "lat": 39.7392,
        "lon": -104.9903,
        "match_total": 1,
    }
    row.update(over)
    return row


class TestMapEndpoint:
    async def test_requires_bbox(self, client):
        ac, _ = client
        r = await ac.get("/api/commercial/map")
        assert r.status_code == 422  # bbox is a required query param

    async def test_invalid_bbox_rejected(self, client):
        ac, conn = client
        r = await ac.get("/api/commercial/map", params={"bbox": "not,a,bbox,x"})
        assert r.status_code == 422
        conn.fetch.assert_not_called()

    async def test_empty_featurecollection(self, client):
        ac, conn = client
        conn.fetch.return_value = []
        r = await ac.get("/api/commercial/map", params={"bbox": BBOX, "zoom": 6})
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "FeatureCollection"
        assert body["metadata"] == {
            "total": 0,
            "returned": 0,
            "capped": False,
            "cap": 6000,
            "clustered": False,
            "zoom": 6,
        }
        assert body["features"] == []

    async def test_bbox_and_active_sql(self, client):
        ac, conn = client
        await ac.get("/api/commercial/map", params={"bbox": BBOX, "zoom": 6})
        sql = conn.fetch.call_args.args[0]
        assert "FROM commercial.jobs_raw" in sql
        assert "JOIN commercial.job_locations lo" in sql
        assert "ON lo.source = m.source AND lo.ext_id = m.ext_id" in sql
        assert ACTIVE in sql
        assert "lo.lat IS NOT NULL AND lo.lon IS NOT NULL" in sql
        # No content filters -> bbox binds $1..$4 (lon between W/E, lat between S/N).
        assert "lo.lon BETWEEN $1 AND $3" in sql
        assert "lo.lat BETWEEN $2 AND $4" in sql
        assert conn.fetch.call_args.args[1:] == BBOX_COORDS

    async def test_filter_params_shift_bbox_placeholders(self, client):
        ac, conn = client
        await ac.get(
            "/api/commercial/map",
            params={"bbox": BBOX, "zoom": 6, "loc": ["Denver, CO"]},
        )
        sql = conn.fetch.call_args.args[0]
        # loc binds $1; bbox continues at $2..$5, coherent with the content filter.
        assert f"{_LABEL} = ANY($1)" in sql
        assert "lo.lon BETWEEN $2 AND $4" in sql
        assert "lo.lat BETWEEN $3 AND $5" in sql
        assert conn.fetch.call_args.args[1:] == (["Denver, CO"], *BBOX_COORDS)

    async def test_facet_and_bbox_combine(self, client):
        ac, conn = client
        await ac.get(
            "/api/commercial/map",
            params={"bbox": BBOX, "clearance": ["Secret"], "exclude_ncr": 1},
        )
        sql = conn.fetch.call_args.args[0]
        assert "data->>'securityClearanceRequirement' = ANY($1)" in sql
        assert "lo.locality_area IS DISTINCT FROM $2" in sql  # job-level NCR
        assert "lo.lon BETWEEN $3 AND $5" in sql
        assert "lo.lat BETWEEN $4 AND $6" in sql
        # Point-level NCR filter binds after the bbox so a surviving multi-site
        # job drops its DC pin but keeps its non-DC ones.
        assert "lo.locality_area IS DISTINCT FROM $7" in sql
        assert conn.fetch.call_args.args[1:] == (
            ["Secret"],
            NCR_LOCALITY,
            *BBOX_COORDS,
            NCR_LOCALITY,
        )

    async def test_point_shape(self, client):
        ac, conn = client
        conn.fetch.return_value = [_map_row(salary_min="90000", salary_max="120000")]
        r = await ac.get("/api/commercial/map", params={"bbox": BBOX, "zoom": 6})
        body = r.json()
        assert body["metadata"]["total"] == 1
        feat = body["features"][0]
        assert feat["type"] == "Feature"
        assert feat["geometry"] == {
            "type": "Point",
            "coordinates": [-104.9903, 39.7392],
        }
        assert feat["properties"] == {
            "ext_id": "1000001",
            "url": "https://www.clearancejobs.com/jobs/1000001/systems-engineer",
            "title": "Systems Engineer",
            "company": "Acme Defense",
            "clearance": "Secret",
            "salary_min": 90000,
            "salary_max": 120000,
            "location": "Denver, CO",
        }

    async def test_no_user_values_in_map_sql(self, client):
        ac, conn = client
        await ac.get(
            "/api/commercial/map",
            params={
                "bbox": BBOX,
                "company": "Acme Defense",
                "q": "engineer",
                "loc": "Denver, CO",
            },
        )
        sql = conn.fetch.call_args.args[0]
        for literal in ("Acme Defense", "engineer", "Denver, CO"):
            assert literal not in sql
