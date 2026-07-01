"""Tests for GET /api/jobs/list, the shared filter builders, and clustering branches."""

import pytest

from backend.api.jobs import (
    DOD_DEPARTMENTS,
    NCR_LOCALITY,
    SORT_COLUMNS,
    _build_content_filters,
    _build_filters,
    _build_list_filters,
)


# --- Unit tests: shared filter builders ---


class TestBuildListFilters:
    """_build_list_filters has no bbox params; every value goes through $N placeholders."""

    def test_empty(self):
        where, params = _build_list_filters()
        assert where == ""
        assert params == []

    def test_each_value_has_placeholder(self):
        where, params = _build_list_filters(
            agency="DOD",
            department="Department of the Navy",
            grade_min=5,
            grade_max=12,
            salary_min=50000,
            salary_max=120000,
            clearance="Secret",
            keyword="analyst",
            state="Virginia",
            country="United States",
            city="Arlington",
            series="0343",
            locality="Rest of U.S.",
        )
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"missing placeholder ${i}"

    def test_no_user_values_in_sql(self):
        where, _ = _build_list_filters(
            agency="DOD",
            clearance="Secret",
            keyword="analyst",
            state="Virginia",
            salary_min=50000,
        )
        for literal in ("DOD", "Secret", "analyst", "Virginia", "50000"):
            assert literal not in where

    def test_placeholders_are_sequential(self):
        where, params = _build_list_filters(agency="A", clearance="B", series="C")
        # 3 simple equality filters -> $1,$2,$3 with no gaps
        assert "$1" in where and "$2" in where and "$3" in where
        assert "$4" not in where
        assert len(params) == 3


class TestBuildContentFiltersBranches:
    """Exercise every branch of the shared content-filter builder."""

    def test_department_dod_all_expands(self):
        where, params = _build_content_filters(
            [], [], department="Department of Defense (All)"
        )
        assert "department IN (" in where
        assert params == DOD_DEPARTMENTS
        # one placeholder per DOD department
        for i in range(1, len(DOD_DEPARTMENTS) + 1):
            assert f"${i}" in where

    def test_department_single(self):
        where, params = _build_content_filters(
            [], [], department="Department of the Navy"
        )
        assert "department = $1" in where
        assert params == ["Department of the Navy"]

    def test_grade_min_max(self):
        where, params = _build_content_filters([], [], grade_min=5, grade_max=12)
        assert "gs_max >= $1" in where
        assert "gs_min <= $2" in where
        assert params == [5, 12]

    def test_salary_cast_to_str(self):
        where, params = _build_content_filters(
            [], [], salary_min=50000, salary_max=120000
        )
        assert "max_salary::numeric >= $1::numeric" in where
        assert "min_salary::numeric <= $2::numeric" in where
        # salary values are bound as strings
        assert params == ["50000", "120000"]

    def test_keyword_uses_tsquery(self):
        where, params = _build_content_filters([], [], keyword="nurse")
        assert "plainto_tsquery" in where
        assert params == ["nurse"]

    def test_state_remote(self):
        where, params = _build_content_filters([], [], state="Remote")
        assert "remote = true" in where
        assert params == []

    def test_state_telework(self):
        where, params = _build_content_filters([], [], state="Telework Eligible")
        assert "telework = true" in where
        assert params == []

    def test_state_plain(self):
        where, params = _build_content_filters([], [], state="Maryland")
        assert "state = $1" in where
        assert params == ["Maryland"]

    def test_country_city_series_locality(self):
        where, params = _build_content_filters(
            [],
            [],
            country="United States",
            city="Arlington",
            series="0610",
            locality="Rest of U.S.",
        )
        assert "country = $1" in where
        assert "location_name = $2" in where
        assert "series_code = $3" in where
        assert "locality_area = $4" in where
        assert params == ["United States", "Arlington", "0610", "Rest of U.S."]

    def test_exclude_ncr(self):
        where, params = _build_content_filters([], [], exclude_ncr=True)
        assert "locality_area IS DISTINCT FROM $1" in where
        assert params == [NCR_LOCALITY]

    def test_agency(self):
        where, params = _build_content_filters([], [], agency="National Park Service")
        assert "org = $1" in where
        assert params == ["National Park Service"]

    def test_clearance(self):
        where, params = _build_content_filters([], [], clearance="Top Secret")
        assert "clearance = $1" in where
        assert params == ["Top Secret"]

    def test_with_seed_bbox_clauses_offsets_placeholders(self):
        """When seeded with bbox clauses (4 params), filter placeholders continue from $5."""
        where, params = _build_filters(
            -77.5, 38.5, -76.5, 39.5, agency="DOD", clearance="Secret"
        )
        assert "geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)" in where
        assert "org = $5" in where
        assert "clearance = $6" in where
        assert len(params) == 6


# --- Integration tests: GET /api/jobs/list ---


class TestJobsListResponse:
    """Response envelope and per-job shape."""

    async def test_envelope(self, client):
        r = await client.get("/api/jobs/list")
        assert r.status_code == 200
        data = r.json()
        for key in ("total", "page", "per_page", "pages", "jobs"):
            assert key in data
        assert isinstance(data["total"], int)
        assert isinstance(data["jobs"], list)
        assert data["page"] == 1
        assert data["per_page"] == 25

    async def test_pages_computed(self, client):
        r = await client.get("/api/jobs/list", params={"per_page": 10})
        data = r.json()
        total, per_page = data["total"], data["per_page"]
        expected = (total + per_page - 1) // per_page if total else 0
        assert data["pages"] == expected

    async def test_job_shape(self, client):
        r = await client.get("/api/jobs/list", params={"per_page": 5})
        jobs = r.json()["jobs"]
        if not jobs:
            pytest.skip("no jobs returned")
        job = jobs[0]
        for field in (
            "id",
            "title",
            "org",
            "department",
            "salary",
            "grade",
            "clearance",
            "location",
            "close_date",
            "remote",
            "telework",
            "series",
        ):
            assert field in job, f"missing field: {field}"
        assert isinstance(job["remote"], bool)
        assert isinstance(job["telework"], bool)


class TestJobsListPagination:
    async def test_page1_differs_from_page2(self, client):
        # Default close_date sort has many ties/NULLs; the position_id tiebreaker in the
        # outer ORDER BY makes OFFSET paging stable, so adjacent pages never overlap.
        params = {"per_page": 25, "sort": "close_date", "order": "asc"}
        r1 = await client.get("/api/jobs/list", params={**params, "page": 1})
        r2 = await client.get("/api/jobs/list", params={**params, "page": 2})
        d1, d2 = r1.json(), r2.json()
        if d1["total"] <= 25:
            pytest.skip("not enough rows for a second page")
        ids1 = {j["id"] for j in d1["jobs"]}
        ids2 = {j["id"] for j in d2["jobs"]}
        assert ids1.isdisjoint(ids2), "adjacent pages overlap — pagination not stable"
        assert d2["page"] == 2

    async def test_per_page_above_100_rejected(self, client):
        # per_page is bounded le=100 at the query layer -> 422.
        r = await client.get("/api/jobs/list", params={"per_page": 500})
        assert r.status_code == 422

    async def test_per_page_max_100_ok(self, client):
        r = await client.get("/api/jobs/list", params={"per_page": 100})
        assert r.status_code == 200
        assert len(r.json()["jobs"]) <= 100

    async def test_page_zero_rejected(self, client):
        r = await client.get("/api/jobs/list", params={"page": 0})
        assert r.status_code == 422

    async def test_page_past_end_empty(self, client):
        r = await client.get("/api/jobs/list", params={"per_page": 25, "page": 100000})
        assert r.status_code == 200
        assert r.json()["jobs"] == []


class TestJobsListSorting:
    @pytest.mark.parametrize("sort", list(SORT_COLUMNS))
    @pytest.mark.parametrize("order", ["asc", "desc"])
    async def test_sort_order_ok(self, client, sort, order):
        r = await client.get(
            "/api/jobs/list", params={"sort": sort, "order": order, "per_page": 5}
        )
        assert r.status_code == 200
        assert isinstance(r.json()["jobs"], list)

    async def test_invalid_sort_falls_back(self, client):
        # Unknown sort field is mapped to the default column, not an error.
        r = await client.get(
            "/api/jobs/list", params={"sort": "bogus_field", "per_page": 5}
        )
        assert r.status_code == 200

    async def test_invalid_order_defaults_asc(self, client):
        r = await client.get(
            "/api/jobs/list", params={"order": "sideways", "per_page": 5}
        )
        assert r.status_code == 200

    async def test_salary_sort_desc(self, client):
        r = await client.get(
            "/api/jobs/list", params={"sort": "salary", "order": "desc", "per_page": 5}
        )
        assert r.status_code == 200


class TestJobsListFilters:
    """Filters narrow results; assert subset behavior against the unfiltered total."""

    async def _total(self, client, **params):
        r = await client.get("/api/jobs/list", params={"per_page": 1, **params})
        assert r.status_code == 200
        return r.json()["total"]

    async def test_baseline_total(self, client):
        assert await self._total(client) >= 0

    async def test_department_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(client, department="Department of the Navy")
        assert filtered <= base

    async def test_keyword_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(client, keyword="nurse")
        assert filtered <= base

    async def test_series_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(client, series="0610")
        assert filtered <= base

    async def test_grade_salary_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(
            client, grade_min=9, grade_max=13, salary_min=60000, salary_max=150000
        )
        assert filtered <= base

    async def test_city_state_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(
            client, city="Washington", state="District of Columbia"
        )
        assert filtered <= base

    async def test_exclude_ncr_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(client, exclude_ncr=True)
        assert filtered <= base

    async def test_locality_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(client, locality=NCR_LOCALITY)
        assert filtered <= base

    async def test_dod_all_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(client, department="Department of Defense (All)")
        assert filtered <= base

    async def test_clearance_subset(self, client):
        base = await self._total(client)
        filtered = await self._total(client, clearance="Secret")
        assert filtered <= base


# --- /api/jobs clustering branches ---


class TestClustering:
    async def test_low_zoom_conus_clusters(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-130,25,-65,50", "zoom": "4"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["metadata"]["clustered"] is True
        if data["features"]:
            props = data["features"][0]["properties"]
            assert props["cluster"] is True
            assert isinstance(props["point_count"], int)
            assert "top_agency" in props

    async def test_tight_bbox_high_zoom_individual(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.04,38.89,-77.02,38.91", "zoom": "16"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["metadata"]["clustered"] is False
        if data["features"]:
            feat = data["features"][0]
            assert feat["geometry"]["type"] == "Point"
            assert "cluster" not in feat["properties"]
            assert "id" in feat["properties"]
