"""Tests for input validation — bbox, zoom, grade, salary parameters."""


class TestBboxValidation:
    """Bbox must be 4 comma-separated floats: west,south,east,north."""

    async def test_valid_bbox(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,38.5,-76.5,39.5", "zoom": "10"}
        )
        assert r.status_code == 200

    async def test_missing_bbox(self, client):
        r = await client.get("/api/jobs", params={"zoom": "10"})
        assert r.status_code == 422

    async def test_bbox_too_few_values(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,38.5,-76.5", "zoom": "10"}
        )
        assert r.status_code == 422
        assert "4 comma-separated" in r.json()["error"]

    async def test_bbox_too_many_values(self, client):
        r = await client.get("/api/jobs", params={"bbox": "1,2,3,4,5", "zoom": "10"})
        assert r.status_code == 422

    async def test_bbox_non_numeric(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "abc,38.5,-76.5,39.5", "zoom": "10"}
        )
        assert r.status_code == 422

    async def test_bbox_longitude_out_of_range(self, client):
        # Out-of-range longitude is clamped to [-180, 180], not rejected.
        r = await client.get(
            "/api/jobs", params={"bbox": "-200,38.5,-76.5,39.5", "zoom": "10"}
        )
        assert r.status_code == 200

    async def test_bbox_latitude_out_of_range(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,100,-76.5,39.5", "zoom": "10"}
        )
        assert r.status_code == 422
        assert "latitude" in r.json()["error"]

    async def test_bbox_west_greater_than_east(self, client):
        # west >= east is treated as a wrapped map -> full longitude range, not rejected.
        r = await client.get(
            "/api/jobs", params={"bbox": "-76.5,38.5,-77.5,39.5", "zoom": "10"}
        )
        assert r.status_code == 200

    async def test_bbox_south_greater_than_north(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,39.5,-76.5,38.5", "zoom": "10"}
        )
        assert r.status_code == 422
        assert "south" in r.json()["error"]


class TestZoomValidation:
    """Zoom must be integer 0-18."""

    async def test_missing_zoom(self, client):
        r = await client.get("/api/jobs", params={"bbox": "-77.5,38.5,-76.5,39.5"})
        assert r.status_code == 422

    async def test_zoom_negative(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,38.5,-76.5,39.5", "zoom": "-1"}
        )
        assert r.status_code == 422

    async def test_zoom_too_high(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.5,38.5,-76.5,39.5", "zoom": "19"}
        )
        assert r.status_code == 422

    async def test_zoom_zero(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-170,-80,170,80", "zoom": "0"}
        )
        assert r.status_code == 200

    async def test_zoom_eighteen(self, client):
        r = await client.get(
            "/api/jobs", params={"bbox": "-77.04,38.89,-77.03,38.90", "zoom": "18"}
        )
        assert r.status_code == 200


class TestGradeValidation:
    """Grade min/max must be integers 1-15."""

    async def test_valid_grade_range(self, client):
        r = await client.get(
            "/api/jobs",
            params={
                "bbox": "-130,25,-65,50",
                "zoom": "4",
                "grade_min": "5",
                "grade_max": "12",
            },
        )
        assert r.status_code == 200

    async def test_grade_min_zero(self, client):
        r = await client.get(
            "/api/jobs",
            params={"bbox": "-130,25,-65,50", "zoom": "4", "grade_min": "0"},
        )
        assert r.status_code == 422

    async def test_grade_max_above_15(self, client):
        r = await client.get(
            "/api/jobs",
            params={"bbox": "-130,25,-65,50", "zoom": "4", "grade_max": "16"},
        )
        assert r.status_code == 422

    async def test_grade_non_integer(self, client):
        r = await client.get(
            "/api/jobs",
            params={"bbox": "-130,25,-65,50", "zoom": "4", "grade_min": "abc"},
        )
        assert r.status_code == 422


class TestSalaryValidation:
    """Salary min/max must be non-negative integers."""

    async def test_valid_salary_range(self, client):
        r = await client.get(
            "/api/jobs",
            params={
                "bbox": "-130,25,-65,50",
                "zoom": "4",
                "salary_min": "50000",
                "salary_max": "120000",
            },
        )
        assert r.status_code == 200

    async def test_salary_negative(self, client):
        r = await client.get(
            "/api/jobs",
            params={"bbox": "-130,25,-65,50", "zoom": "4", "salary_min": "-1"},
        )
        assert r.status_code == 422

    async def test_salary_non_integer(self, client):
        r = await client.get(
            "/api/jobs",
            params={"bbox": "-130,25,-65,50", "zoom": "4", "salary_min": "abc"},
        )
        assert r.status_code == 422
