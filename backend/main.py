"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from backend.api.config import router as config_router
from backend.api.filters import router as filters_router
from backend.api.health import router as health_router
from backend.api.jobs import router as jobs_router
from backend.db import close_pool, create_pool

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool()
    yield
    await close_pool()


app = FastAPI(title="USAJobs Map", lifespan=lifespan, docs_url=None, redoc_url=None)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://maps.googleapis.com "
        "https://maps.gstatic.com https://unpkg.com; "
        "img-src 'self' data: https://*.googleapis.com "
        "https://*.gstatic.com https://*.basemaps.cartocdn.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "font-src 'self' https://unpkg.com"
    )
    # Static assets (HTML/JS/CSS) carry an ETag; force revalidation so a redeploy
    # is picked up on the next ordinary reload instead of serving a stale cache.
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "code": 500},
    )


# API routes
app.include_router(health_router)
app.include_router(config_router)
app.include_router(filters_router)
app.include_router(jobs_router)

# Static files (frontend only — never project root)
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
