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
- `cj_collect.py` — ClearanceJobs ingester into the separate `commercial`
  schema (sitemap-driven sightings + rate-limited detail fetches; collected
  data is personal-use only and never republished).
- `migrations/` — ordered SQL migrations (01–12). `schema.sql` is a snapshot
  of the live schema; usable as the baseline for a fresh database (see
  Database setup).
- `scripts/analyze_jobs.py` — offline posting analysis via the Claude API
  (install the analysis extra: `pip install -e ".[analysis]"`; needs
  `DATABASE_URL` and `ANTHROPIC_API_KEY` in the environment).
- `tests/` — pytest suite.

## Database setup

Requires PostgreSQL 17 with PostGIS. Two setup paths:

- Fresh database: create the `usajobs` database and the two app roles, then
  apply `schema.sql` as the baseline — it is a snapshot of the live schema
  (regenerated 2026-07-02) and already includes everything migrations 01–12
  produce. Do not also run the migrations afterwards.
- Existing database: apply `migrations/01` … `migrations/12` in order and skip
  `schema.sql`.

Fresh-database walkthrough (as a superuser):

```sh
psql -h localhost -U postgres -c "CREATE ROLE usajobs LOGIN PASSWORD '...'"
psql -h localhost -U postgres -c "CREATE ROLE usajobs_web LOGIN PASSWORD '...'"
psql -h localhost -U postgres -c "CREATE ROLE usajobs_collector LOGIN PASSWORD '...'"
psql -h localhost -U postgres -c "CREATE DATABASE usajobs OWNER usajobs"
psql -h localhost -U usajobs -d usajobs -f schema.sql
```

Roles: `usajobs` owns the schema and runs migrations; `usajobs_web` is the
read-only role the FastAPI app connects as; `usajobs_collector` is the writer
role for `collect.py` (it refreshes `jobs_geo` via the SECURITY DEFINER
function `refresh_jobs_geo()`). The connection URLs in `.env` use the web and
collector roles.

Two lookup tables ship empty and need one-time loads before `jobs_geo` can
resolve counties and locality pay areas (without them the view still builds,
but `fips`/`locality_area` are NULL and the locality filter is empty):

- `locality_areas` (county FIPS → OPM locality pay area): run
  `python parse_localities.py`, which parses the saved OPM
  "Locality Pay Area Definitions" HTML in the repo root and writes
  `/tmp/locality_areas.sql`. That file also contains DROP/CREATE TABLE
  statements that will fail once `jobs_geo` exists (the view depends on the
  table), so apply only the inserts:
  `grep '^INSERT' /tmp/locality_areas.sql | psql -h localhost -U usajobs -d usajobs`.
  Live it holds 925 FIPS rows across 57 locality areas.
- `us_counties` (county polygons, `geometry(MultiPolygon, 4326)`): load US
  county boundaries into the columns `fips` (5-digit FIPS), `name`,
  `state_fips`, `geom` — e.g. from the Census cartographic boundary counties
  shapefile via `ogr2ogr`/`shp2pgsql`. The exact command originally used is
  not preserved in the repo; the live table holds 3,221 counties. Any source
  with correct 5-digit FIPS and WGS84 multipolygons works.

Finally, populate the materialized view. The first refresh must be
non-concurrent (do the lookup loads first so the refresh picks them up):

```sh
psql -h localhost -U usajobs -d usajobs -c "REFRESH MATERIALIZED VIEW jobs_geo"
```

Subsequent refreshes happen automatically: the collector calls
`refresh_jobs_geo()` after each sweep.

## Running

1. Copy `.env.example` to `.env` and fill in the database URLs and API keys.
   If Postgres runs on the Docker host, set the DB host in `.env` to
   `host.docker.internal` (compose maps it to the host gateway).
2. Set up the database (see above).
3. `docker compose up -d --build` — the app is served on port 8080.
4. Schedule the collector hourly:
   `docker compose run --rm app python collect.py --full`.

## Tests

`uv sync --extra dev` (or `pip install -e ".[dev]"`), then `pytest`. The
API-backed tests need a reachable database.
