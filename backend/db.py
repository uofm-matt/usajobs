"""Async database connection pool using asyncpg."""

import asyncio
import logging
from urllib.parse import urlparse

import asyncpg

from backend.config import settings

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None


async def create_pool(max_retries: int = 5, retry_delay: float = 2.0) -> asyncpg.Pool:
    """Create the asyncpg connection pool with retry loop for startup."""
    global pool
    parsed = urlparse(settings.database_url_web)

    for attempt in range(1, max_retries + 1):
        try:
            pool = await asyncpg.create_pool(
                host=parsed.hostname,
                port=parsed.port or 5432,
                database=parsed.path.lstrip("/"),
                user=parsed.username,
                password=parsed.password,
                min_size=2,
                max_size=10,
                server_settings={"statement_timeout": "10000"},
            )
            logger.info("Database pool created (attempt %d)", attempt)
            return pool
        except (asyncpg.PostgresError, OSError) as e:
            if attempt == max_retries:
                raise
            logger.warning(
                "DB connect attempt %d failed: %s — retrying in %.0fs",
                attempt,
                e,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)


async def close_pool() -> None:
    """Close the connection pool."""
    global pool
    if pool:
        await pool.close()
        pool = None
        logger.info("Database pool closed")


def get_pool() -> asyncpg.Pool:
    """Return the current pool, raising if not initialized."""
    if pool is None:
        raise RuntimeError("Database pool not initialized")
    return pool
