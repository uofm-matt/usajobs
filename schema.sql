-- USAJobs schema: materialized view, indexes, roles, refresh tracking
-- Reference schema documenting intended structure; migrations/01-07 are the applied history and have already diverged from this file (see jobs_geo below). Do not run this file against a database that has already applied those migrations.

-- ============================================================
-- Tables (created by collect.py, included here for reference)
-- ============================================================

CREATE TABLE IF NOT EXISTS jobs_raw (
    position_id     TEXT PRIMARY KEY,
    data            JSONB NOT NULL,
    first_seen      TIMESTAMPTZ DEFAULT NOW(),
    last_seen       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs_history (
    id              SERIAL PRIMARY KEY,
    position_id     TEXT NOT NULL,
    data            JSONB NOT NULL,
    captured_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_raw_data ON jobs_raw USING gin(data);
CREATE INDEX IF NOT EXISTS idx_jobs_history_pid ON jobs_history(position_id);
CREATE INDEX IF NOT EXISTS idx_jobs_history_captured ON jobs_history(captured_at);

-- ============================================================
-- Materialized view: jobs_geo (one row per job per location)
-- ============================================================

-- TODO: live jobs_geo (migrations 02/04/06) has additional columns (series_code, series_name, fips, locality_area, is_evergreen) and exclusion filters (JobCategory, evergreen-org, recency, close-date) not reflected here; needs a follow-up migration to reconcile.

DROP MATERIALIZED VIEW IF EXISTS jobs_geo;

CREATE MATERIALIZED VIEW jobs_geo AS
SELECT DISTINCT ON (j.position_id, loc.value ->> 'LocationName')
    j.position_id,
    loc.value ->> 'LocationName' AS location_name,
    loc.value ->> 'CityName' AS city_name,
    loc.value ->> 'CountrySubDivisionCode' AS state,
    loc.value ->> 'CountryCode' AS country,
    CASE WHEN (loc.value ->> 'Latitude')::numeric = 0 THEN 38.89 ELSE (loc.value ->> 'Latitude')::numeric END AS lat,
    CASE WHEN (loc.value ->> 'Longitude')::numeric = 0 THEN -77.03 ELSE (loc.value ->> 'Longitude')::numeric END AS lon,
    ST_SetSRID(ST_MakePoint(
        CASE WHEN (loc.value ->> 'Longitude')::numeric = 0 THEN -77.03 ELSE (loc.value ->> 'Longitude')::double precision END,
        CASE WHEN (loc.value ->> 'Latitude')::numeric = 0 THEN 38.89 ELSE (loc.value ->> 'Latitude')::double precision END
    ), 4326) AS geom,
    (j.data -> 'MatchedObjectDescriptor') ->> 'PositionTitle' AS title,
    (j.data -> 'MatchedObjectDescriptor') ->> 'OrganizationName' AS org,
    (j.data -> 'MatchedObjectDescriptor') ->> 'DepartmentName' AS department,
    ((j.data -> 'MatchedObjectDescriptor') -> 'PositionRemuneration' -> 0) ->> 'MinimumRange' AS min_salary,
    ((j.data -> 'MatchedObjectDescriptor') -> 'PositionRemuneration' -> 0) ->> 'MaximumRange' AS max_salary,
    ((j.data -> 'MatchedObjectDescriptor') -> 'PositionRemuneration' -> 0) ->> 'RateIntervalCode' AS rate_interval,
    ((j.data -> 'MatchedObjectDescriptor') -> 'UserArea' -> 'Details') ->> 'SecurityClearance' AS clearance,
    ((j.data -> 'MatchedObjectDescriptor') -> 'JobGrade' -> 0) ->> 'Code' AS pay_plan,
    ((j.data -> 'MatchedObjectDescriptor') -> 'UserArea' -> 'Details') ->> 'LowGrade' AS low_grade,
    ((j.data -> 'MatchedObjectDescriptor') -> 'UserArea' -> 'Details') ->> 'HighGrade' AS high_grade,
    -- gs_equivalent_range() is defined in migrations/01_gs_equivalent.sql and superseded by migrations/05_gs_equivalent_nongs_null.sql (current behavior); not redefined here.
    (gs_equivalent_range(
        ((j.data -> 'MatchedObjectDescriptor') -> 'JobGrade' -> 0) ->> 'Code',
        ((j.data -> 'MatchedObjectDescriptor') -> 'UserArea' -> 'Details') ->> 'LowGrade'
    ))[1] AS gs_min,
    (gs_equivalent_range(
        ((j.data -> 'MatchedObjectDescriptor') -> 'JobGrade' -> 0) ->> 'Code',
        ((j.data -> 'MatchedObjectDescriptor') -> 'UserArea' -> 'Details') ->> 'HighGrade'
    ))[2] AS gs_max,
    COALESCE(((j.data -> 'MatchedObjectDescriptor') -> 'UserArea' -> 'Details') ->> 'RemoteIndicator' = 'true', false) AS remote,
    COALESCE(((j.data -> 'MatchedObjectDescriptor') -> 'UserArea' -> 'Details') ->> 'TeleworkEligible' = 'true', false) AS telework,
    (j.data -> 'MatchedObjectDescriptor') ->> 'ApplicationCloseDate' AS close_date,
    j.first_seen,
    j.last_seen
FROM jobs_raw j,
    LATERAL jsonb_array_elements(
        (j.data -> 'MatchedObjectDescriptor') -> 'PositionLocation'
    ) AS loc(value)
WHERE
    (loc.value ->> 'Latitude') IS NOT NULL
    AND (loc.value ->> 'Longitude') IS NOT NULL
;

-- ============================================================
-- Indexes on jobs_geo
-- ============================================================

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY
CREATE UNIQUE INDEX idx_jobs_geo_pk ON jobs_geo (position_id, location_name);

-- Spatial
CREATE INDEX idx_jobs_geo_geom ON jobs_geo USING gist(geom);

-- Text search
CREATE INDEX idx_jobs_geo_title_fts ON jobs_geo USING gin(to_tsvector('english', title));

-- Filter columns
CREATE INDEX idx_jobs_geo_org ON jobs_geo(org);
CREATE INDEX idx_jobs_geo_clearance ON jobs_geo(clearance);
CREATE INDEX idx_jobs_geo_state ON jobs_geo(state);
CREATE INDEX idx_jobs_geo_country ON jobs_geo(country);
CREATE INDEX idx_jobs_geo_grade ON jobs_geo(low_grade, high_grade);
CREATE INDEX idx_jobs_geo_salary ON jobs_geo(min_salary, max_salary);

-- ============================================================
-- Refresh tracking (for cache invalidation)
-- ============================================================

CREATE TABLE IF NOT EXISTS refresh_log (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    last_refresh    TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO refresh_log (id, last_refresh)
VALUES (1, NOW())
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Database roles
-- ============================================================

-- Web role: read-only access for the FastAPI app
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'usajobs_web') THEN
        CREATE ROLE usajobs_web LOGIN PASSWORD 'usajobs_web';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE usajobs TO usajobs_web;
GRANT USAGE ON SCHEMA public TO usajobs_web;
GRANT SELECT ON jobs_geo, jobs_raw, refresh_log TO usajobs_web;

-- Collector role: write access for collect.py
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'usajobs_collector') THEN
        CREATE ROLE usajobs_collector LOGIN PASSWORD 'usajobs_collector';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE usajobs TO usajobs_collector;
GRANT USAGE ON SCHEMA public TO usajobs_collector;
GRANT SELECT, INSERT, UPDATE ON jobs_raw, jobs_history TO usajobs_collector;
GRANT USAGE, SELECT ON SEQUENCE jobs_history_id_seq TO usajobs_collector;
GRANT SELECT, UPDATE ON refresh_log TO usajobs_collector;
-- Collector needs to refresh the materialized view
GRANT ALL ON jobs_geo TO usajobs_collector;
