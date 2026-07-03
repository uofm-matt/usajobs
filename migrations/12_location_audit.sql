-- Migration 12: commercial.location_audit — durable location-verification results
--
-- ClearanceJobs' structured jobLocation sometimes tags a US job with a same-named
-- foreign city ("Melbourne, FL" -> "Melbourne, Australia"). scripts/verify_oconus.py
-- adjudicates the ambiguous OCONUS cases with a model cascade and records the verdict
-- here, one row per checked posting. For a confirmed 'mislabel' with a real location,
-- cj_collect._write_job_locations reads this table and geocodes the corrected place
-- instead of the wrong structured one — so the repair survives every future refetch,
-- and the audit doubles as the "already checked" marker so verification stays
-- incremental. The LLM pass runs where the OAuth token lives; applying the override
-- is pure SQL, so the server cron re-applies fixes without needing model access.
--
-- Apply as owner: psql -h localhost -U usajobs -d usajobs -f migrations/12_location_audit.sql

BEGIN;

CREATE TABLE IF NOT EXISTS commercial.location_audit (
    source        TEXT NOT NULL,
    ext_id        TEXT NOT NULL,
    verdict       TEXT NOT NULL,          -- correct | mislabel | unclear
    confidence    TEXT,                   -- high | medium | low
    real_city     TEXT,                   -- corrected location (mislabel only)
    real_region   TEXT,
    real_country  TEXT,
    reason        TEXT,
    model         TEXT,                   -- ladder tier that settled it
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, ext_id)
);

GRANT SELECT, INSERT, UPDATE ON commercial.location_audit TO usajobs_collector;
GRANT SELECT ON commercial.location_audit TO usajobs_web;

COMMIT;
