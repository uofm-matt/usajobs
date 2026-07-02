-- Migration 08: 'commercial' schema for the personal-use commercial cleared-jobs layer
--
-- Stands up a schema separate from public so the commercial layer (cleared jobs
-- harvested from ATS/aggregators) never mingles with the USAJobs data. Source #1
-- is ClearanceJobs: companies from its company sitemap, jobs from its job-posting
-- sitemap enriched with schema.org JobPosting detail pages. This data is personal
-- use only and is never republished; only the collector code is public.
--
-- Apply as owner: psql -h localhost -U usajobs -d usajobs -f migrations/08_commercial_schema.sql
-- Runs as the owner role 'usajobs'; the schema is owned by usajobs, and the
-- collector/web roles get only the narrow grants at the bottom.

BEGIN;

CREATE SCHEMA IF NOT EXISTS commercial;

-- One row per employer. cj_profile_url is the ClearanceJobs company page and the
-- natural key for upserts; connector names the ATS harvester ('greenhouse',
-- 'lever', ...) resolved later, 'unresolved' until then.
CREATE TABLE IF NOT EXISTS commercial.companies (
    id                SERIAL PRIMARY KEY,
    name              TEXT NOT NULL,
    name_normalized   TEXT,
    cj_profile_url    TEXT UNIQUE,
    careers_url       TEXT,
    connector         TEXT NOT NULL DEFAULT 'unresolved',
    connector_params  JSONB,
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    last_sweep        TIMESTAMPTZ,
    notes             TEXT
);

-- One row per posting, keyed by (source, ext_id). Sitemap sweeps insert id-only
-- rows and maintain sightings; data stays NULL until the detail page is fetched.
-- consecutive_misses mirrors public.jobs_raw (migration 07) for sighting-gap
-- tracking against the sitemap.
CREATE TABLE IF NOT EXISTS commercial.jobs_raw (
    source              TEXT NOT NULL,
    ext_id              TEXT NOT NULL,
    company_id          INTEGER REFERENCES commercial.companies(id),
    url                 TEXT,
    slug                TEXT,
    data                JSONB,
    first_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_at          TIMESTAMPTZ,
    consecutive_misses  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, ext_id)
);

CREATE INDEX IF NOT EXISTS idx_commercial_jobs_raw_company
    ON commercial.jobs_raw(company_id);

-- Prior detail payloads captured when a posting's data changes.
CREATE TABLE IF NOT EXISTS commercial.jobs_history (
    id           SERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    ext_id       TEXT NOT NULL,
    data         JSONB,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Postings that reappeared in the sitemap after being missed by one or more
-- sweeps — the commercial analogue of public.sighting_returns.
CREATE TABLE IF NOT EXISTS commercial.sighting_returns (
    id             SERIAL PRIMARY KEY,
    source         TEXT NOT NULL,
    ext_id         TEXT NOT NULL,
    missed_sweeps  INTEGER NOT NULL,
    returned_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- CJ posting <-> corporate posting links. Stays empty until P4 wires up
-- CJ-to-corporate matching; created now so grants land in one place.
CREATE TABLE IF NOT EXISTS commercial.job_matches (
    cj_ext_id    TEXT NOT NULL,
    corp_source  TEXT NOT NULL,
    corp_ext_id  TEXT NOT NULL,
    confidence   REAL,
    method       TEXT,
    PRIMARY KEY (cj_ext_id, corp_source, corp_ext_id)
);

-- ============================================================
-- Grants
-- ============================================================

GRANT USAGE ON SCHEMA commercial TO usajobs_collector, usajobs_web;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA commercial TO usajobs_collector;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA commercial TO usajobs_collector;

GRANT SELECT ON ALL TABLES IN SCHEMA commercial TO usajobs_web;

COMMIT;
