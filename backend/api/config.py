"""Config endpoint — serves non-secret config to frontend."""

from fastapi import APIRouter

from backend.config import settings

router = APIRouter()


@router.get("/api/config")
async def get_config():
    return {
        "google_maps_api_key": settings.google_maps_api_key,
    }
