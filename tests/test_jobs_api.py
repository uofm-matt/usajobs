"""Tests for /api/jobs and /api/jobs/{id} — response format, parameterization safety."""

import inspect
import re

import pytest

from backend.api.jobs import _build_filters, _fetch_clusters, _fetch_individual


# --- Parameterization safety ---


class TestParameterization:
    """Verify SQL is built with $N placeholders, never string interpolation of user values."""

    def test_build_filters_uses_dollar_placeholders(self):
        """All dynamic values must be passed as bind params via $N."""
        where, params = _build_filters(
            -77.5,
            38.5,
            -76.5,
            39.5,
            agency="DOD",
            grade_min=5,
            grade_max=12,
            salary_min=50000,
            salary_max=120000,
            clearance="Secret",
            keyword="analyst",
            state="Virginia",
            country="United States",
        )

        # Every param value should have a corresponding $N placeholder
        for i in range(1, len(params) + 1):
            assert f"${i}" in where, f"Missing placeholder ${i} in WHERE clause"

        # No user values should appear literally in the SQL
        assert "DOD" not in where
        assert "Secret" not in where
        assert "analyst" not in where
        assert "Virginia" not in where
        assert "50000" not in where

    def test_build_filters_bbox_only(self):
        """With no optional filters, only bbox params are used."""
        where, params = _build_filters(-77.5, 38.5, -76.5, 39.5)
        assert len(params) == 4
        assert "$1" in where and "$4" in where
        assert "$5" not in where

    def test_no_fstring_interpolation_in_sql(self):
        """Source code of query functions must not interpolate user filter values."""
        for fn in [_fetch_individual, _fetch_clusters]:
            source = inspect.getsource(fn)
            # Should not have f-string interpolation of filter variable names
            # (allowed: {where}, {MAX_INDIVIDUAL}, {next_param} which are internal)
            fstring_vars = re.findall(r"\{(\w+)\}", source)
            dangerous = {
                "agency",
                "keyword",
                "clearance",
                "state",
                "country",
                "grade_min",
                "grade_max",
                "salary_min",
                "salary_max",
            }
            found = dangerous & set(fstring_vars)
            assert not found, f"{fn.__name__} interpolates user values: {found}"


# --- Response format ---


class TestJobsResponseFormat:
    """Verify GeoJSON structure from /api/jobs."""

    async def test_geojson_structure(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,38.5,-76.5,39.5", "zoom": "10"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "FeatureCollection"
        assert "metadata" in data
        assert "features" in data
        assert isinstance(data["features"], list)

    async def test_metadata_fields(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,38.5,-76.5,39.5", "zoom": "10"}
        )
        meta = r.json()["metadata"]
        assert "total" in meta
        assert "clustered" in meta
        assert "zoom" in meta
        assert isinstance(meta["total"], int)
        assert isinstance(meta["clustered"], bool)

    async def test_individual_point_properties(self, client):
        """At high zoom with narrow bbox, features should be individual points."""
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.04,38.89,-77.02,38.91", "zoom": "14"}
        )
        data = r.json()
        if data["features"]:
            feat = data["features"][0]
            assert feat["type"] == "Feature"
            assert feat["geometry"]["type"] == "Point"
            assert len(feat["geometry"]["coordinates"]) == 2
            props = feat["properties"]
            assert "id" in props
            assert "title" in props
            assert "org" in props

    async def test_filters_applied(self, client):
        """Keyword filter should restrict results."""
        r_all = await client.get(
            "/api/jobs", params={"bbox": "-130,25,-65,50", "zoom": "4"}
        )
        r_filtered = await client.get(
            "/api/jobs",
            params={"bbox": "-130,25,-65,50", "zoom": "4", "keyword": "nurse"},
        )
        total_all = r_all.json()["metadata"]["total"]
        total_filtered = r_filtered.json()["metadata"]["total"]
        assert total_filtered <= total_all


class TestJobDetail:
    async def test_not_found(self, client):
        r = await client.get("/api/jobs/NONEXISTENT-999")
        assert r.status_code == 404
        assert r.json()["error"] == "Job not found"

    async def test_detail_fields(self, client):
        """Fetch a real job ID from the jobs endpoint and verify detail fields."""
        # Use a tight bbox at high zoom to get individual (non-clustered) points
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.04,38.88,-77.00,38.92", "zoom": "15"}
        )
        data = r.json()
        if not data["features"] or data["metadata"]["clustered"]:
            pytest.skip("No individual points in test viewport")

        pid = data["features"][0]["properties"]["id"]
        r = await client.get(f"/api/jobs/{pid}")
        assert r.status_code == 200

        job = r.json()
        for field in [
            "id",
            "title",
            "org",
            "department",
            "salary",
            "grade",
            "locations",
            "clearance",
            "open_date",
            "close_date",
        ]:
            assert field in job, f"Missing field: {field}"
        assert isinstance(job["locations"], list)


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
