"""Shared fixtures for tests — async HTTP client backed by the real FastAPI app + DB."""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import backend.db as db_mod
from backend.db import create_pool, close_pool
from backend.main import app


@pytest_asyncio.fixture
async def client():
    """Async test client — creates DB pool per test function."""
    if db_mod.pool is None:
        await create_pool()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    if db_mod.pool is not None:
        await close_pool()
