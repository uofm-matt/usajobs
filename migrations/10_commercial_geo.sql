-- Migration 10: commercial gazetteer + per-posting geocoded locations
--
-- Two GeoNames-derived reference tables plus a per-posting location table for the
-- commercial layer. The gazetteer tables (geo_cities, geo_zips) are loaded by
-- scripts/load_geonames.py from GeoNames dumps (CC-BY: https://www.geonames.org/);
-- they change only when that loader reruns. commercial.job_locations is rewritten
-- per fetch by cj_collect.py — it parses each posting's locations, normalizes
-- country/region/city, and geocodes against the gazetteer (US zip, then city
-- fallback), storing lat/lon and which path hit in geocode_method.
--
-- Combined display label convention (facets + UI): country 'United States' renders
-- "City, RG" (USPS region code); otherwise "City, Country". Rows with no city are
-- skipped for labels.
--
-- Apply as owner: psql -h localhost -U usajobs -d usajobs -f migrations/10_commercial_geo.sql

BEGIN;

-- GeoNames cities15000 gazetteer (every city with population > 15000). Keyed by the
-- GeoNames id; admin1 is the source admin-1 code (NULL where GeoNames ships none).
CREATE TABLE IF NOT EXISTS commercial.geo_cities (
    geonameid   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    ascii_name  TEXT NOT NULL,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    country     TEXT NOT NULL,
    admin1      TEXT,
    population  INTEGER
);

-- Case-insensitive city lookup scoped by country + admin1 — the 'city' geocode path.
CREATE INDEX IF NOT EXISTS idx_commercial_geo_cities_lookup
    ON commercial.geo_cities (lower(ascii_name), country, admin1);

-- GeoNames US postal gazetteer. Keyed by 5-digit zip; state is the USPS 2-letter code.
CREATE TABLE IF NOT EXISTS commercial.geo_zips (
    zip    TEXT PRIMARY KEY,
    place  TEXT,
    state  TEXT,
    lat    DOUBLE PRECISION NOT NULL,
    lon    DOUBLE PRECISION NOT NULL
);

-- One row per parsed location on a commercial posting, rewritten per fetch by
-- cj_collect.py. seq orders multiple locations on one posting; lat/lon are NULL when
-- geocoding found no match, and geocode_method records the path that hit ('zip'/'city').
CREATE TABLE IF NOT EXISTS commercial.job_locations (
    source          TEXT NOT NULL,
    ext_id          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    city            TEXT,
    region          TEXT,
    country         TEXT,
    postal          TEXT,
    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    geocode_method  TEXT,
    PRIMARY KEY (source, ext_id, seq),
    FOREIGN KEY (source, ext_id) REFERENCES commercial.jobs_raw (source, ext_id)
);

CREATE INDEX IF NOT EXISTS idx_commercial_job_locations_latlon
    ON commercial.job_locations (lat, lon);

CREATE INDEX IF NOT EXISTS idx_commercial_job_locations_place
    ON commercial.job_locations (country, region, city);

-- ============================================================
-- Grants
-- ============================================================

GRANT SELECT ON commercial.geo_cities, commercial.geo_zips
    TO usajobs_collector, usajobs_web;

GRANT SELECT ON commercial.job_locations TO usajobs_web;
GRANT SELECT, INSERT, UPDATE, DELETE ON commercial.job_locations TO usajobs_collector;

COMMIT;
