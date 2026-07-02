"""Tests for /api/commercial/jobs — fully mocked, no live DB, no network.

Unit tests cover the WHERE builder and item shaping; endpoint tests drive the
route through the ASGI app with the asyncpg pool replaced by an AsyncMock.
"""

import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock

from backend.api.commercial import _build_where, _item, _locations, _num
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

    async def test_limit_over_cap_rejected(self, client):
        ac, _ = client
        r = await ac.get("/api/commercial/jobs", params={"limit": 101})
        assert r.status_code == 422
