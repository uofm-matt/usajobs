-- Migration 07: capture sighting gaps in the ingester
--
-- Goal: size the jobs_geo recency window from data instead of inference. We can't
-- currently tell whether a posting that vanished from search ever came back,
-- because jobs_raw only keeps the latest last_seen. This adds a per-job miss
-- counter the full-sweep collector maintains, plus a log of "returns".
--
-- collect.py (full sweep only) now: bumps consecutive_misses for recently-active
-- jobs it didn't see this sweep, resets it to 0 when a job reappears, and writes a
-- sighting_returns row whenever a job returns after a gap (consecutive_misses > 0).
-- A counter that climbs and then resets is a *measured* vanish-then-return.
--
-- Apply as owner: psql -h localhost -U usajobs -d usajobs -f migrations/07_sighting_tracking.sql

ALTER TABLE jobs_raw ADD COLUMN IF NOT EXISTS consecutive_misses INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS sighting_returns (
    id            SERIAL PRIMARY KEY,
    position_id   TEXT NOT NULL,
    missed_sweeps INTEGER NOT NULL,   -- consecutive full sweeps the job was absent before returning
    returned_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sighting_returns_at ON sighting_returns(returned_at);

-- Collector writes returns + maintains the counter (it already has UPDATE on jobs_raw,
-- which covers the new column). Web may read the log.
GRANT INSERT, SELECT ON sighting_returns TO usajobs_collector;
GRANT USAGE, SELECT ON SEQUENCE sighting_returns_id_seq TO usajobs_collector;
GRANT SELECT ON sighting_returns TO usajobs_web;
