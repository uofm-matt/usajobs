-- Hide jobs past their application deadline from the public app.
--
-- Migration 03 added a recency filter (last_seen within 2 days) that drops jobs
-- which vanished from USAJobs search. But a job can still be returned by the API
-- (last_seen current) after its ApplicationCloseDate has passed, so the deadline
-- could lapse while the pin stayed on the map. This adds an availability filter:
-- a job appears only while it is still open for application.
--
-- "Open for application" = ApplicationCloseDate is absent/empty, or its date is
-- today or later. close_date is stored as an ISO end-of-day timestamp
-- (e.g. 2026-06-14T23:59:59.9970), so comparing ::date >= current_date keeps a
-- job visible through its entire closing day rather than hiding it at midnight.
--
-- Retention is unaffected: jobs_raw and jobs_history still keep every job and
-- every version forever. This only changes what the materialized view surfaces.
--
-- Same safe pattern as migration 03: read the live definition and wrap it rather
-- than transcribing it, all in one transaction so any error rolls back and leaves
-- the live view untouched. Indexes, owner, and grant are recreated to match.
--
-- Apply as superuser (owner DDL + ALTER OWNER):
--   docker exec -i postgres psql -U usajobs -d usajobs -v ON_ERROR_STOP=1 -f - < migrations/04_jobs_geo_application_open.sql

BEGIN;

DO $mig$
DECLARE v_def text;
BEGIN
    SELECT pg_get_viewdef('jobs_geo'::regclass, true) INTO v_def;
    v_def := regexp_replace(v_def, ';\s*$', '');
    DROP MATERIALIZED VIEW jobs_geo;
    EXECUTE
        'CREATE MATERIALIZED VIEW jobs_geo AS SELECT * FROM (' || v_def ||
        ') _open WHERE NULLIF(_open.close_date, '''') IS NULL '
        'OR (_open.close_date)::date >= current_date WITH DATA';
END
$mig$;

ALTER MATERIALIZED VIEW jobs_geo OWNER TO usajobs;

CREATE UNIQUE INDEX idx_jobs_geo_pk ON public.jobs_geo USING btree (position_id, location_name);
CREATE INDEX idx_jobs_geo_geom ON public.jobs_geo USING gist (geom);
CREATE INDEX idx_jobs_geo_title_fts ON public.jobs_geo USING gin (to_tsvector('english'::regconfig, title));
CREATE INDEX idx_jobs_geo_org ON public.jobs_geo USING btree (org);
CREATE INDEX idx_jobs_geo_clearance ON public.jobs_geo USING btree (clearance);
CREATE INDEX idx_jobs_geo_state ON public.jobs_geo USING btree (state);
CREATE INDEX idx_jobs_geo_country ON public.jobs_geo USING btree (country);
CREATE INDEX idx_jobs_geo_grade ON public.jobs_geo USING btree (gs_min, gs_max);
CREATE INDEX idx_jobs_geo_salary ON public.jobs_geo USING btree (min_salary, max_salary);
CREATE INDEX idx_jobs_geo_series ON public.jobs_geo USING btree (series_code);
CREATE INDEX idx_jobs_geo_locality ON public.jobs_geo USING btree (locality_area);
CREATE INDEX idx_jobs_geo_fips ON public.jobs_geo USING btree (fips);

GRANT SELECT ON jobs_geo TO usajobs_web;

COMMIT;
