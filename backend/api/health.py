"""Health check endpoint."""

from fastapi import APIRouter, Response

from backend.db import get_pool

router = APIRouter()


@router.get("/api/health")
async def health(response: Response):
    try:
        pool = get_pool()
        async with pool.acquire(timeout=2) as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok"}
    except Exception:
        response.status_code = 503
        return {"status": "unavailable"}
