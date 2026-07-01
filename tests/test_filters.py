"""Tests for /api/filters endpoint — response structure."""


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
