-- Migration 09: hot-path index for the commercial jobs list query
--
-- The /api/commercial/jobs list endpoint pages active CJ postings ordered by
-- datePosted. Without support the planner seq-scans commercial.jobs_raw and
-- detoasts every data-bearing document to evaluate the ORDER BY key before the
-- top-N sort — multi-second per page at the post-backfill steady state (~39k
-- data-bearing rows, 5-30KB documents). This partial expression index matches
-- the base predicate and sort so the page is served from the index.
--
-- The predicate below must stay in lockstep with the API base predicate in
-- backend/api/commercial.py (_build_where): source = 'clearancejobs',
-- data IS NOT NULL, consecutive_misses = 0. Diverging silently drops the index.
--
-- Apply as owner: psql -h localhost -U usajobs -d usajobs -f migrations/09_commercial_indexes.sql

BEGIN;

CREATE INDEX IF NOT EXISTS idx_commercial_jobs_active_posted
    ON commercial.jobs_raw ((data->>'datePosted') DESC NULLS LAST, ext_id)
    WHERE source = 'clearancejobs' AND data IS NOT NULL AND consecutive_misses = 0;

COMMIT;
