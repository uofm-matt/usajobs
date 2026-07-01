-- Let the least-privileged collector role refresh jobs_geo without owning it.
-- REFRESH MATERIALIZED VIEW requires ownership of the view; this SECURITY DEFINER
-- function runs as its definer (the matview owner, usajobs) and is the only thing
-- the collector is granted to call.
-- Apply as the owner:  psql -h localhost -U usajobs -d usajobs -f migrations/02_refresh_jobs_geo.sql

CREATE OR REPLACE FUNCTION refresh_jobs_geo() RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY jobs_geo;
    UPDATE refresh_log SET last_refresh = NOW() WHERE id = 1;
END;
$$;

REVOKE ALL ON FUNCTION refresh_jobs_geo() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_jobs_geo() TO usajobs_collector;
