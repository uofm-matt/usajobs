-- Migration 11: OPM locality area on commercial.job_locations
--
-- Replaces the hand-maintained NCR city list with the authoritative, county-based
-- definition the federal side already uses. Each geocoded job location is resolved
-- (point-in-polygon on public.us_counties) to its county FIPS and OPM locality-pay
-- area, materialized here the same way public.jobs_geo materializes locality_area.
-- exclude_ncr then filters on locality_area = the DC-Baltimore-Arlington locality,
-- spelling-independent (Fort Meade vs "Fort George G Meade" no longer matters).
--
-- The collector (usajobs_collector) needs to read the geography lookups to resolve
-- localities during geocoding, so grant it SELECT on them here.
--
-- Apply as owner: psql -h localhost -U usajobs -d usajobs -f migrations/11_commercial_locality.sql

BEGIN;

ALTER TABLE commercial.job_locations
    ADD COLUMN IF NOT EXISTS county_fips    TEXT,
    ADD COLUMN IF NOT EXISTS locality_area  TEXT;

CREATE INDEX IF NOT EXISTS idx_commercial_job_locations_locality
    ON commercial.job_locations(locality_area);

GRANT SELECT ON public.us_counties, public.locality_areas TO usajobs_collector;

COMMIT;
