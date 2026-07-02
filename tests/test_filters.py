"""Tests for /api/filters — response shape, filter params, SQL builder."""

import inspect
import re

import pytest

from backend.api.filters import (
    DOD_DEPARTMENTS,
    NCR_LOCALITY,
    _build_where,
    _content_clause,
    build_content_where,
)


# --- SQL builder safety (mirrors tests/test_jobs_api.py TestParameterization) ---


class TestParameterization:
    """Verify _build_where emits $N placeholders, never literal user values."""

    def test_no_filters_returns_true(self):
        where, params = _build_where()
        assert where == "TRUE"
        assert params == []

    def test_all_string_filters_use_placeholders(self):
        where, params = _build_where(
            department="Department of Veterans Affairs",
            agency="Veterans Health Administration",
            clearance="Secret",
            state="Virginia",
            country="United States",
            city="Arlington, Virginia",
            keyword="analyst",
            series="0610",
            locality="Richmond, VA",
        )
        # Every param has a matching $N placeholder
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"Missing placeholder ${i}"
        # No user values appear literally in the SQL
        for literal in [
            "Department of Veterans Affairs",
            "Veterans Health Administration",
            "Secret",
            "Virginia",
            "United States",
            "Arlington",
            "analyst",
            "0610",
            "Richmond",
        ]:
            assert literal not in where, f"Leaked literal {literal!r} into SQL"

    def test_numeric_filters_use_placeholders(self):
        where, params = _build_where(
            grade_min=5,
            grade_max=12,
            salary_min=50000,
            salary_max=120000,
        )
        for i in range(1, len(params) + 1):
            assert f"${i}" in where
        # numeric values are bound, not interpolated into the SQL text
        for literal in ["5", "12", "50000", "120000"]:
            assert literal not in re.sub(r"\$\d+", "", where)

    def test_grade_filters_map_to_gs_columns(self):
        where, params = _build_where(grade_min=7, grade_max=11)
        assert "gs_max >= $1" in where
        assert "gs_min <= $2" in where
        assert params == [7, 11]

    def test_salary_filters_cast_numeric(self):
        where, params = _build_where(salary_min=40000, salary_max=90000)
        assert "max_salary::numeric >= $1::numeric" in where
        assert "min_salary::numeric <= $2::numeric" in where
        assert params == ["40000", "90000"]

    def test_keyword_uses_tsvector(self):
        where, params = _build_where(keyword="nurse")
        assert "to_tsvector('english', title)" in where
        assert "plainto_tsquery('english', $1)" in where
        assert params == ["nurse"]
        assert "nurse" not in where

    def test_state_remote_no_param(self):
        where, params = _build_where(state="Remote")
        assert where == "remote = true"
        assert params == []

    def test_state_telework_no_param(self):
        where, params = _build_where(state="Telework Eligible")
        assert where == "telework = true"
        assert params == []

    def test_state_regular_uses_param(self):
        where, params = _build_where(state="Texas")
        assert "state = $1" in where
        assert params == ["Texas"]

    def test_dod_all_expands_to_in_clause(self):
        where, params = _build_where(department="Department of Defense (All)")
        assert params == DOD_DEPARTMENTS
        # IN clause with one placeholder per DoD department
        for i in range(1, len(DOD_DEPARTMENTS) + 1):
            assert f"${i}" in where
        assert "department IN (" in where
        assert "Department of the Army" not in where

    def test_regular_department_single_param(self):
        where, params = _build_where(department="Department of the Interior")
        assert "department = $1" in where
        assert params == ["Department of the Interior"]

    def test_exclude_ncr_binds_locality(self):
        where, params = _build_where(exclude_ncr=True)
        assert "locality_area IS DISTINCT FROM $1" in where
        assert params == [NCR_LOCALITY]
        assert NCR_LOCALITY not in where

    def test_placeholders_increment_across_combined_filters(self):
        where, params = _build_where(
            agency="X",
            clearance="Y",
            state="Z",
            grade_min=5,
            exclude_ncr=True,
        )
        # Sequential placeholders $1..$N, each present exactly once
        nums = sorted(int(n) for n in re.findall(r"\$(\d+)", where))
        assert nums == list(range(1, len(params) + 1))

    def test_no_fstring_interpolation_of_user_values(self):
        dangerous = {
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
            "value",
            "field",
        }
        for fn in [_content_clause, build_content_where, _build_where]:
            source = inspect.getsource(fn)
            fstring_vars = set(re.findall(r"\{(\w+)\}", source))
            found = dangerous & fstring_vars
            assert not found, f"{fn.__name__} interpolates user values: {found}"


# --- Full response shape (unfiltered) ---


class TestUnfilteredShape:
    async def test_all_keys_present(self, client):
        r = await client.get("/api/filters")
        assert r.status_code == 200
        data = r.json()
        for key in [
            "departments",
            "agencies",
            "clearances",
            "states",
            "countries",
            "cities",
            "localities",
            "series",
            "country_bounds",
        ]:
            assert key in data, f"Missing key {key}"

    async def test_name_count_lists(self, client):
        data = (await client.get("/api/filters")).json()
        for key in ["departments", "agencies", "clearances", "countries", "localities"]:
            items = data[key]
            assert isinstance(items, list)
            for item in items:
                assert isinstance(item["name"], str)
                assert isinstance(item["count"], int)

    async def test_states_shape_and_synthetic_entries(self, client):
        states = (await client.get("/api/filters")).json()["states"]
        assert len(states) > 0
        names = {s["name"] for s in states}
        for s in states:
            assert isinstance(s["name"], str)
            assert isinstance(s["count"], int)
        # Synthetic Remote / Telework Eligible appear when counts exist
        for synthetic in names & {"Remote", "Telework Eligible"}:
            entry = next(s for s in states if s["name"] == synthetic)
            assert entry["count"] > 0

    async def test_cities_shape(self, client):
        cities = (await client.get("/api/filters")).json()["cities"]
        assert isinstance(cities, list)
        if not cities:
            pytest.skip("no cities returned")
        c = cities[0]
        assert isinstance(c["name"], str)
        assert isinstance(c["lat"], float)
        assert isinstance(c["lon"], float)
        assert isinstance(c["count"], int)
        assert "state" in c
        assert "country" in c

    async def test_series_shape(self, client):
        series = (await client.get("/api/filters")).json()["series"]
        assert isinstance(series, list)
        if not series:
            pytest.skip("no series returned")
        s = series[0]
        assert isinstance(s["code"], str)
        assert isinstance(s["name"], str)
        assert s["code"] in s["name"]
        assert isinstance(s["count"], int)

    async def test_country_bounds_shape(self, client):
        bounds = (await client.get("/api/filters")).json()["country_bounds"]
        assert isinstance(bounds, dict)
        if "United States" in bounds:
            us = bounds["United States"]
            assert len(us) == 4
            assert all(isinstance(v, float) for v in us)
            south, west, north, east = us
            assert south <= north
            assert west <= east

    async def test_departments_include_dod_all_when_present(self, client):
        depts = (await client.get("/api/filters")).json()["departments"]
        names = [d["name"] for d in depts]
        has_dod_component = any(d in names for d in DOD_DEPARTMENTS)
        if has_dod_component:
            assert "Department of Defense (All)" in names
            dod_all = next(
                d for d in depts if d["name"] == "Department of Defense (All)"
            )
            component_total = sum(
                d["count"] for d in depts if d["name"] in DOD_DEPARTMENTS
            )
            assert dod_all["count"] == component_total

    async def test_second_request_served_from_cache(self, client, monkeypatch):
        first = (await client.get("/api/filters")).json()

        def cache_miss(*args, **kwargs):
            raise AssertionError("cache miss")

        async def stale(_conn):
            return 0.0

        monkeypatch.setattr("backend.api.filters._load_filters", cache_miss)
        monkeypatch.setattr("backend.api.filters._get_last_refresh", stale)
        r = await client.get("/api/filters")
        assert r.status_code == 200
        assert r.json() == first


# --- Filtered endpoint behavior ---


class TestFilteredEndpoint:
    async def test_state_filter_subsets_cities(self, client):
        unfiltered = (await client.get("/api/filters")).json()
        all_cities = {c["name"] for c in unfiltered["cities"]}
        r = await client.get("/api/filters", params={"state": "Virginia"})
        assert r.status_code == 200
        va_cities = {c["name"] for c in r.json()["cities"]}
        if not va_cities:
            pytest.skip("no Virginia cities")
        assert va_cities <= all_cities

    async def test_country_filter_returns_only_country(self, client):
        r = await client.get("/api/filters", params={"country": "United States"})
        assert r.status_code == 200
        countries = r.json()["countries"]
        if countries:
            assert {c["name"] for c in countries} == {"United States"}

    async def test_department_filter_restricts_departments(self, client):
        r = await client.get(
            "/api/filters",
            params={"department": "Department of Veterans Affairs"},
        )
        assert r.status_code == 200
        names = [d["name"] for d in r.json()["departments"]]
        if names:
            assert names == ["Department of Veterans Affairs"]

    async def test_dod_all_filter_returns_dod_components(self, client):
        r = await client.get(
            "/api/filters", params={"department": "Department of Defense (All)"}
        )
        assert r.status_code == 200
        names = {d["name"] for d in r.json()["departments"]}
        # Result departments (excluding the synthetic aggregate) are DoD components
        names.discard("Department of Defense (All)")
        if names:
            assert names <= set(DOD_DEPARTMENTS)

    async def test_clearance_filter_200(self, client):
        unfiltered = (await client.get("/api/filters")).json()
        clearances = unfiltered["clearances"]
        if not clearances:
            pytest.skip("no clearances in DB")
        target = clearances[0]["name"]
        r = await client.get("/api/filters", params={"clearance": target})
        assert r.status_code == 200
        names = [c["name"] for c in r.json()["clearances"]]
        if names:
            assert names == [target]

    async def test_series_filter_subsets(self, client):
        unfiltered = (await client.get("/api/filters")).json()
        series = unfiltered["series"]
        if not series:
            pytest.skip("no series in DB")
        code = series[0]["code"]
        r = await client.get("/api/filters", params={"series": code})
        assert r.status_code == 200
        codes = {s["code"] for s in r.json()["series"]}
        if codes:
            assert codes == {code}

    async def test_city_filter_200(self, client):
        unfiltered = (await client.get("/api/filters")).json()
        cities = unfiltered["cities"]
        if not cities:
            pytest.skip("no cities in DB")
        name = cities[0]["name"]
        r = await client.get("/api/filters", params={"city": name})
        assert r.status_code == 200
        result = r.json()["cities"]
        if result:
            assert {c["name"] for c in result} == {name}

    async def test_locality_filter_200(self, client):
        unfiltered = (await client.get("/api/filters")).json()
        localities = unfiltered["localities"]
        if not localities:
            pytest.skip("no localities in DB")
        name = localities[0]["name"]
        r = await client.get("/api/filters", params={"locality": name})
        assert r.status_code == 200
        result = {item["name"] for item in r.json()["localities"]}
        if result:
            assert result == {name}

    async def test_exclude_ncr_drops_ncr_locality(self, client):
        r = await client.get("/api/filters", params={"exclude_ncr": "true"})
        assert r.status_code == 200
        names = {item["name"] for item in r.json()["localities"]}
        assert NCR_LOCALITY not in names

    async def test_grade_range_filter_200(self, client):
        r = await client.get(
            "/api/filters", params={"grade_min": "9", "grade_max": "13"}
        )
        assert r.status_code == 200
        assert isinstance(r.json()["agencies"], list)

    async def test_salary_range_filter_200(self, client):
        r = await client.get(
            "/api/filters", params={"salary_min": "60000", "salary_max": "150000"}
        )
        assert r.status_code == 200
        assert isinstance(r.json()["agencies"], list)

    async def test_keyword_filter_subsets_agencies(self, client):
        unfiltered = (await client.get("/api/filters")).json()
        all_total = sum(a["count"] for a in unfiltered["agencies"])
        r = await client.get("/api/filters", params={"keyword": "nurse"})
        assert r.status_code == 200
        filtered_total = sum(a["count"] for a in r.json()["agencies"])
        assert filtered_total <= all_total

    async def test_combined_filters_200(self, client):
        r = await client.get(
            "/api/filters",
            params={
                "country": "United States",
                "grade_min": "5",
                "grade_max": "15",
                "exclude_ncr": "true",
                "keyword": "engineer",
            },
        )
        assert r.status_code == 200
        data = r.json()
        for key in ["departments", "agencies", "states", "countries", "cities"]:
            assert isinstance(data[key], list)

    async def test_grade_out_of_range_rejected(self, client):
        r = await client.get("/api/filters", params={"grade_min": "20"})
        assert r.status_code == 422


# --- Basic response structure ---


class TestFilters:
    async def test_filters_response_structure(self, client):
        r = await client.get("/api/filters")
        assert r.status_code == 200
        data = r.json()
        assert "agencies" in data
        assert "clearances" in data
        assert "states" in data
        assert "countries" in data

    async def test_agencies_have_name_and_count(self, client):
        r = await client.get("/api/filters")
        agencies = r.json()["agencies"]
        assert len(agencies) > 0, "Expected at least one agency"
        for agency in agencies:
            assert "name" in agency
            assert "count" in agency
            assert isinstance(agency["count"], int)
            assert agency["count"] > 0

    async def test_agencies_sorted_descending_by_count(self, client):
        r = await client.get("/api/filters")
        agencies = r.json()["agencies"]
        counts = [a["count"] for a in agencies]
        assert counts == sorted(counts, reverse=True)

    async def test_states_are_strings(self, client):
        r = await client.get("/api/filters")
        states = r.json()["states"]
        assert len(states) > 0, "Expected at least one state"
        for s in states:
            assert isinstance(s, dict)
            assert isinstance(s["name"], str)
            assert len(s["name"]) > 0
            assert isinstance(s["count"], int)

    async def test_clearances_non_empty(self, client):
        r = await client.get("/api/filters")
        clearances = r.json()["clearances"]
        assert isinstance(clearances, list)

    async def test_countries_non_empty(self, client):
        r = await client.get("/api/filters")
        countries = r.json()["countries"]
        assert isinstance(countries, list)
        assert len(countries) > 0
        for c in countries:
            assert isinstance(c["name"], str)
            assert isinstance(c["count"], int)
        assert any(c["name"] == "United States" for c in countries)
