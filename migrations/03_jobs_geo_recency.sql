-- Hide closed jobs from the public app.
--
-- jobs_geo previously contained every job in jobs_raw, open or closed, so ~52% of
-- the pins on the public map were stale postings (last seen weeks ago, no longer
-- in USAJobs search). This adds a recency filter: only jobs confirmed within the
-- last 2 days (48 hourly sweeps) appear. The 2-day window tolerates a missed run
-- or two without emptying the map; the freshness healthcheck covers longer gaps.
--
-- Postgres has no CREATE OR REPLACE for materialized views, so we drop and rebuild.
-- Rather than transcribe the ~50-line definition (and risk a typo), we read the
-- live definition and wrap it: SELECT * FROM (<existing def>) WHERE last_seen >= ...
-- The whole swap runs in one transaction, so any error rolls back and leaves the
-- live view untouched. Indexes, owner, and grant are recreated to match the original.
--
-- Also seeds refresh_log id=1 — refresh_jobs_geo() does UPDATE ... WHERE id=1, which
-- was a silent no-op because the row never existed.
--
-- Apply as superuser (owner DDL + ALTER OWNER):
--   docker exec -i postgres psql -U postgres -d usajobs -v ON_ERROR_STOP=1 -f - < migrations/03_jobs_geo_recency.sql

BEGIN;

INSERT INTO refresh_log (id, last_refresh)
SELECT 1, NOW() WHERE NOT EXISTS (SELECT 1 FROM refresh_log WHERE id = 1);

DO $mig$
DECLARE v_def text;
BEGIN
    SELECT pg_get_viewdef('jobs_geo'::regclass, true) INTO v_def;
    v_def := regexp_replace(v_def, ';\s*$', '');
    DROP MATERIALIZED VIEW jobs_geo;
    EXECUTE
        'CREATE MATERIALIZED VIEW jobs_geo AS SELECT * FROM (' || v_def ||
        ') _src WHERE _src.last_seen >= (now() - ''2 days''::interval) WITH DATA';
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
