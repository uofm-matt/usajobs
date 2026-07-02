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
    NCR_CITIES,
    _build_where,
    _item,
    _locations,
    _num,
    _sort_exprs,
)
from backend.main import app

BASE = [
    "source = 'clearancejobs'",
    "data IS NOT NULL",
    "consecutive_misses = 0",
]


# --- WHERE builder ---


class TestBuildWhere:
    def test_base_predicate_only(self):
        where, params = _build_where()
        assert where == " AND ".join(BASE)
        assert params == []

    def test_every_param_has_placeholder(self):
        where, params = _build_where(
            clearance="Secret",
            company="Acme Defense",
            country="United States",
            q="engineer",
            salary_min=90000,
        )
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_no_user_values_in_sql(self):
        where, _ = _build_where(
            clearance="Secret",
            company="Acme Defense",
            country="United States",
            q="engineer",
            salary_min=90000,
        )
        for literal in ("Secret", "Acme Defense", "United States", "engineer", "90000"):
            assert literal not in where

    def test_clearance_ilike(self):
        where, params = _build_where(clearance="Secret")
        assert "data->>'securityClearanceRequirement' ILIKE $1" in where
        assert params == ["Secret"]

    def test_company_wrapped_in_wildcards(self):
        where, params = _build_where(company="Valida")
        assert "data->'hiringOrganization'->>'name' ILIKE $1" in where
        assert params == ["%Valida%"]

    def test_country_uses_lax_jsonpath(self):
        where, params = _build_where(country="United States")
        # Lax jsonpath + [*] handles jobLocation as a list or a single object.
        assert "jsonb_path_exists(data" in where
        assert "$.jobLocation[*].address.addressCountry ? (@ == $c)" in where
        assert "jsonb_build_object('c', $1::text)" in where
        assert params == ["United States"]

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
        where, params = _build_where(clearance="Secret", company="X", q="y")
        assert "ILIKE $1" in where  # clearance
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

    def test_location_placeholder_sequential_after_other_filters(self):
        where, params = _build_where(
            clearance="Secret", company="X", q="y", location="Denver"
        )
        assert "ILIKE $1" in where  # clearance
        assert "$2" in where  # company
        assert "$3" in where  # keyword (title + slug)
        assert "ILIKE $4" in where  # location locality + region
        assert params == ["Secret", "%X%", "%y%", "%Denver%"]
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_exclude_ncr_off_by_default_leaves_sql_unchanged(self):
        # Default (unchecked) must not touch the base predicate or bind a param.
        where, params = _build_where()
        assert where == " AND ".join(BASE)
        assert params == []
        assert "= ANY(" not in where

    def test_exclude_ncr_keeps_jobs_with_any_non_ncr_location(self):
        where, params = _build_where(exclude_ncr=True)
        # Keep a job unless EVERY location is NCR: EXISTS a location that is NOT
        # (DC, or a VA/MD locality in the city list).
        assert "jsonb_typeof(data->'jobLocation') = 'array'" in where
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in where
        assert "WHERE NOT (" in where
        assert "COALESCE(loc->'address'->>'addressRegion', '') = 'DC'" in where
        assert "IN ('VA', 'MD')" in where
        # Single bound param carrying the whole city list, matched with = ANY.
        assert (
            "lower(COALESCE(loc->'address'->>'addressLocality', '')) = ANY($1)" in where
        )
        assert params == [NCR_CITIES]

    def test_exclude_ncr_placeholder_sequential_after_other_filters(self):
        where, params = _build_where(
            clearance="Secret", company="X", q="y", location="Denver", exclude_ncr=True
        )
        assert "ILIKE $1" in where  # clearance
        assert "$2" in where  # company
        assert "$3" in where  # keyword (title + slug)
        assert "ILIKE $4" in where  # location locality + region
        assert "= ANY($5)" in where  # NCR city list
        assert params == ["Secret", "%X%", "%y%", "%Denver%", NCR_CITIES]
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_exclude_ncr_city_list_is_lowercase(self):
        # The predicate compares lower(locality); the list must already be lowercase.
        assert NCR_CITIES == [c.lower() for c in NCR_CITIES]


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

    async def test_country_filter_uses_jsonpath(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"country": "United States"})
        sql = conn.fetch.call_args.args[0]
        assert "jsonb_path_exists(data" in sql
        assert conn.fetch.call_args.args[1:] == ("United States",)

    async def test_location_filter_binds_wildcarded_param(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"location": "Denver"})
        sql = conn.fetch.call_args.args[0]
        assert "jsonb_array_elements" in sql
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in sql
        assert "ILIKE $1" in sql
        assert conn.fetch.call_args.args[1:] == ("%Denver%",)

    async def test_exclude_ncr_binds_city_list_param(self, client):
        ac, conn = client
        await ac.get("/api/commercial/jobs", params={"exclude_ncr": 1})
        sql = conn.fetch.call_args.args[0]
        assert "WHERE NOT (" in sql
        assert "IN ('VA', 'MD')" in sql
        assert "= ANY($1)" in sql
        # One param: the city list, bound to both the count and list queries.
        assert conn.fetchval.call_args.args[1:] == (NCR_CITIES,)
        assert conn.fetch.call_args.args[1:] == (NCR_CITIES,)

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
        inner = f"ORDER BY {_sort_exprs('data')[field]} {direction} NULLS LAST, ext_id"
        outer = (
            f"ORDER BY {_sort_exprs('j.data')[field]} {direction} NULLS LAST, j.ext_id"
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
        }

    async def test_sql_targets_active_predicate(self, client):
        ac, conn = client
        await ac.get("/api/commercial/filters")
        clearance_sql, country_sql = (c.args[0] for c in conn.fetch.call_args_list)
        assert ACTIVE in clearance_sql
        assert ACTIVE in country_sql
        assert "securityClearanceRequirement" in clearance_sql
        # Country facet normalizes list/single-object jobLocation like the filter.
        assert "addressCountry" in country_sql
        assert "jsonb_array_elements" in country_sql
        assert "ELSE jsonb_build_array(data->'jobLocation') END" in country_sql
