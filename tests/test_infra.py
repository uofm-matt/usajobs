"""Unit tests for DB pool lifecycle (backend.db) and /api/health failure path."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import backend.db as db_mod
from backend.main import app


@pytest.fixture(autouse=True)
def reset_pool():
    """Snapshot/restore the module-level pool so other suites are undisturbed."""
    saved = db_mod.pool
    db_mod.pool = None
    yield
    db_mod.pool = saved


class TestCreatePool:
    async def test_retries_on_oserror_then_succeeds(self, monkeypatch):
        mock_pool = MagicMock()
        calls = []

        async def fake_create_pool(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise OSError("connection refused")
            return mock_pool

        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(db_mod.asyncpg, "create_pool", fake_create_pool)
        monkeypatch.setattr(db_mod.asyncio, "sleep", fake_sleep)

        result = await db_mod.create_pool(max_retries=5, retry_delay=0.01)

        assert result is mock_pool
        assert db_mod.pool is mock_pool
        assert len(calls) == 2
        assert sleeps == [0.01]

    async def test_raises_after_exhausting_retries(self, monkeypatch):
        async def always_fail(**kwargs):
            raise OSError("down")

        async def fake_sleep(delay):
            pass

        monkeypatch.setattr(db_mod.asyncpg, "create_pool", always_fail)
        monkeypatch.setattr(db_mod.asyncio, "sleep", fake_sleep)

        with pytest.raises(OSError):
            await db_mod.create_pool(max_retries=2, retry_delay=0.01)

    async def test_no_retry_when_first_attempt_succeeds(self, monkeypatch):
        mock_pool = MagicMock()
        calls = []

        async def fake_create_pool(**kwargs):
            calls.append(kwargs)
            return mock_pool

        slept = False

        async def fake_sleep(delay):
            nonlocal slept
            slept = True

        monkeypatch.setattr(db_mod.asyncpg, "create_pool", fake_create_pool)
        monkeypatch.setattr(db_mod.asyncio, "sleep", fake_sleep)

        result = await db_mod.create_pool()

        assert result is mock_pool
        assert len(calls) == 1
        assert slept is False


class TestGetPool:
    def test_raises_when_uninitialized(self):
        with pytest.raises(RuntimeError, match="not initialized"):
            db_mod.get_pool()

    def test_returns_pool_when_set(self):
        sentinel = MagicMock()
        db_mod.pool = sentinel
        assert db_mod.get_pool() is sentinel


class TestClosePool:
    async def test_closes_and_nulls_pool(self):
        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()
        db_mod.pool = mock_pool

        await db_mod.close_pool()

        mock_pool.close.assert_awaited_once()
        assert db_mod.pool is None

    async def test_noop_when_pool_is_none(self):
        db_mod.pool = None
        await db_mod.close_pool()
        assert db_mod.pool is None


class TestHealthEndpoint:
    async def test_health_unavailable_when_pool_uninitialized(self):
        """get_pool() raises RuntimeError -> health returns 503/unavailable."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/api/health")

        assert r.status_code == 503
        assert r.json()["status"] == "unavailable"
