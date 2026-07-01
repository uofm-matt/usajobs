"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url_web: str = "postgresql://usajobs_web:CHANGEME@localhost:5432/usajobs"
    database_url_collector: str = (
        "postgresql://usajobs_collector:CHANGEME@localhost:5432/usajobs"
    )

    # USAJobs API
    usajobs_api_key: str = ""
    usajobs_user_agent: str = ""

    # Google Maps
    google_maps_api_key: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
