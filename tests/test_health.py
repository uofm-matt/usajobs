"""Tests for /api/health endpoint."""


class TestHealth:
    async def test_health_ok(self, client):
        r = await client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    async def test_health_returns_json(self, client):
        r = await client.get("/api/health")
        assert "application/json" in r.headers["content-type"]
