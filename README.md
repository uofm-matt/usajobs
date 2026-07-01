# USAJobs Map

A map and search interface for open U.S. federal job postings, backed by a
collector that sweeps the USAJobs Search API on an hourly full sweep.

## What it does

- Ingests open federal postings from the USAJobs Search API, with change
  detection and de-duplication across sweeps.
- Stores them in PostgreSQL/PostGIS and exposes a `jobs_geo` view keyed by
  locality pay area and geography.
- Serves a Leaflet map, faceted filters (department, series, grade, salary,
  clearance, locality, and more), a list view, and per-posting detail — all
  scoped to the current map viewport.

## Stack

- Backend: FastAPI + asyncpg on PostgreSQL 17 / PostGIS.
- Frontend: vanilla ES modules + Leaflet, no build step.
- Collector: `collect.py`, run standalone on a cron.
- Container: `docker compose` (app served on port 8080).

## Layout

- `backend/` — FastAPI app (`main.py`); routers under `api/` (jobs, filters,
  health); asyncpg pool in `db.py`.
- `frontend/` — static `index.html`, `js/`, `css/`.
- `collect.py` — USAJobs API ingester (full sweep).
- `migrations/` — ordered SQL migrations (01–07). `schema.sql` is a reference
  snapshot for reading, not applied DDL.
- `scripts/analyze_jobs.py` — offline posting analysis via the Claude API.
- `tests/` — pytest suite.

## Running

1. Copy `.env.example` to `.env` and fill in the database URLs and API keys.
2. Apply the migrations in order against your database (see `migrations/`).
3. `docker compose up -d --build` — the app is served on port 8080.
4. Schedule the collector hourly:
   `docker compose run --rm app python collect.py --full`.

## Tests

`pytest`. The API-backed tests need a reachable database.
