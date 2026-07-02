-- USAJobs database schema — reference snapshot.
--
-- Regenerated from the live database (pg_dump --schema-only) on 2026-07-02 (MT).
-- migrations/ is the applied history; this file is the current end state of that
-- history, curated for reading. Function bodies and the jobs_geo definition are
-- verbatim from the dump. Safe to apply as the baseline for a FRESH database;
-- do not run it against a database that has already applied migrations/01-08.
--
-- Two tables carry externally sourced data that this file only creates empty:
--   locality_areas — populate with the output of parse_localities.py
--   us_counties    — populate from Census county boundary polygons (see README)
-- jobs_geo joins both; with them empty it still builds, but fips/locality_area
-- come out NULL and the locality filter has nothing to offer.

-- ============================================================
-- Extensions
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- Present live but not required by the app: the production Postgres image is
-- STIG-hardened and preloads pgaudit, so the extension exists in this database.
-- CREATE EXTENSION IF NOT EXISTS pgaudit;

-- ============================================================
-- Base tables
-- ============================================================

-- One row per posting, keyed by USAJobs position id; data is the raw API
-- object. consecutive_misses supports sighting-gap tracking (migration 07).
CREATE TABLE IF NOT EXISTS jobs_raw (
    position_id         TEXT PRIMARY KEY,
    data                JSONB NOT NULL,
    first_seen          TIMESTAMPTZ DEFAULT NOW(),
    last_seen           TIMESTAMPTZ DEFAULT NOW(),
    consecutive_misses  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_raw_data ON jobs_raw USING gin(data);

-- Prior versions of a posting captured when its payload changes.
CREATE TABLE IF NOT EXISTS jobs_history (
    id              SERIAL PRIMARY KEY,
    position_id     TEXT NOT NULL,
    data            JSONB NOT NULL,
    captured_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_history_pid ON jobs_history(position_id);
CREATE INDEX IF NOT EXISTS idx_jobs_history_captured ON jobs_history(captured_at);

-- Postings that reappeared after being missed by one or more sweeps
-- (migration 07) — instruments API flakiness vs. real closures.
CREATE TABLE IF NOT EXISTS sighting_returns (
    id              SERIAL PRIMARY KEY,
    position_id     TEXT NOT NULL,
    missed_sweeps   INTEGER NOT NULL,
    returned_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sighting_returns_at ON sighting_returns(returned_at);

-- Single-row timestamp of the last jobs_geo refresh (cache invalidation).
CREATE TABLE IF NOT EXISTS refresh_log (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    last_refresh    TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO refresh_log (id, last_refresh)
VALUES (1, NOW())
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Geography lookups (created empty here; see header)
-- ============================================================

-- OPM locality pay areas, one row per county FIPS. 925 rows / 57 localities
-- live; generated from the OPM locality-pay-area-definitions HTML by
-- parse_localities.py.
CREATE TABLE IF NOT EXISTS locality_areas (
    fips        TEXT PRIMARY KEY,
    locality    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_locality_areas_locality ON locality_areas(locality);

-- US county boundary polygons (Census shapes; 3,221 rows live). fips is the
-- 5-digit county FIPS, state_fips its 2-digit prefix.
CREATE TABLE IF NOT EXISTS us_counties (
    fips        TEXT PRIMARY KEY,
    name        TEXT,
    state_fips  TEXT,
    geom        geometry(MultiPolygon, 4326)
);

CREATE INDEX IF NOT EXISTS idx_counties_geom ON us_counties USING gist(geom);

-- ============================================================
-- Functions: GS-grade crosswalk (migrations 01 + 05)
-- ============================================================

-- Midpoint GS-equivalent for a pay plan + grade; NULL when there is no
-- defensible mapping (unmapped/non-GS plans).
CREATE OR REPLACE FUNCTION gs_equivalent(pay_plan text, grade text) RETURNS integer
    LANGUAGE plpgsql IMMUTABLE
    AS $_$
BEGIN
    IF grade IS NULL OR grade = '' THEN
        RETURN NULL;
    END IF;

    -- 1:1 GS-equivalent pay plans (same grade numbers)
    IF pay_plan IN ('GS','GL','GG','FG') THEN
        RETURN CASE WHEN grade ~ '^\d+$' THEN grade::integer ELSE NULL END;
    END IF;

    -- DoD AcqDemo: NH, NM (Professional/Supervisory)
    IF pay_plan IN ('NH','NM','NQ') THEN
        RETURN CASE grade
            WHEN '1' THEN 2    -- GS-1 to GS-4, midpoint ~2
            WHEN '01' THEN 2
            WHEN '2' THEN 8    -- GS-5 to GS-11, midpoint ~8
            WHEN '02' THEN 8
            WHEN '3' THEN 12   -- GS-12 to GS-13
            WHEN '03' THEN 12
            WHEN '4' THEN 14   -- GS-14 to GS-15
            WHEN '04' THEN 14
            ELSE NULL
        END;
    END IF;

    -- DoD AcqDemo: NJ (Technical)
    IF pay_plan = 'NJ' THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2   -- GS-1 to GS-4
            WHEN '2' THEN 7    WHEN '02' THEN 7   -- GS-5 to GS-10
            WHEN '3' THEN 12   WHEN '03' THEN 12  -- GS-11 to GS-13
            WHEN '4' THEN 14   WHEN '04' THEN 14  -- GS-14 to GS-15
            ELSE NULL
        END;
    END IF;

    -- DoD AcqDemo: NK (Admin Support)
    IF pay_plan = 'NK' THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2   -- GS-1 to GS-4
            WHEN '2' THEN 6    WHEN '02' THEN 6   -- GS-5 to GS-7
            WHEN '3' THEN 9    WHEN '03' THEN 9   -- GS-8 to GS-10
            WHEN '4' THEN 12   WHEN '04' THEN 12  -- GS-11 to GS-13
            ELSE NULL
        END;
    END IF;

    -- Navy/Lab Demo: ND, DP (Scientists & Engineers)
    IF pay_plan IN ('ND','DP') THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2   -- GS-1 to GS-4
            WHEN '2' THEN 8    WHEN '02' THEN 8   -- GS-5 to GS-11
            WHEN '3' THEN 12   WHEN '03' THEN 12  -- GS-12 to GS-13
            WHEN '4' THEN 14   WHEN '04' THEN 14  -- GS-14 to GS-15
            WHEN '5' THEN 15   WHEN '05' THEN 15  -- Above GS-15
            WHEN '6' THEN 15   WHEN '06' THEN 15  -- Distinguished
            ELSE NULL
        END;
    END IF;

    -- Navy Demo: NT, DS (Technical)
    IF pay_plan IN ('NT','DS') THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2
            WHEN '2' THEN 7    WHEN '02' THEN 7
            WHEN '3' THEN 12   WHEN '03' THEN 12
            WHEN '4' THEN 14   WHEN '04' THEN 14
            WHEN '5' THEN 15   WHEN '05' THEN 15
            WHEN '6' THEN 15   WHEN '06' THEN 15
            ELSE NULL
        END;
    END IF;

    -- Navy Demo: NO, DB (Admin)
    IF pay_plan IN ('NO','DB') THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2
            WHEN '2' THEN 6    WHEN '02' THEN 6
            WHEN '3' THEN 9    WHEN '03' THEN 9
            WHEN '4' THEN 12   WHEN '04' THEN 12
            WHEN '5' THEN 15   WHEN '05' THEN 15
            ELSE NULL
        END;
    END IF;

    -- Lab Demo: DA (Admin Professional)
    IF pay_plan = 'DA' THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2
            WHEN '2' THEN 8    WHEN '02' THEN 8
            WHEN '3' THEN 12   WHEN '03' THEN 12
            WHEN '4' THEN 14   WHEN '04' THEN 14
            WHEN '5' THEN 15   WHEN '05' THEN 15
            ELSE NULL
        END;
    END IF;

    -- NIST: ZP, ZA (Professional/Admin)
    IF pay_plan IN ('ZP','ZA') THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2
            WHEN '2' THEN 8    WHEN '02' THEN 8
            WHEN '3' THEN 12   WHEN '03' THEN 12
            WHEN '4' THEN 14   WHEN '04' THEN 14
            WHEN '5' THEN 15   WHEN '05' THEN 15
            ELSE NULL
        END;
    END IF;

    -- NIST: ZT, ZS (Technician/Support)
    IF pay_plan IN ('ZT','ZS') THEN
        RETURN CASE grade
            WHEN '1' THEN 2    WHEN '01' THEN 2
            WHEN '2' THEN 6    WHEN '02' THEN 6
            WHEN '3' THEN 10   WHEN '03' THEN 10
            WHEN '4' THEN 12   WHEN '04' THEN 12
            ELSE NULL
        END;
    END IF;

    -- FAA (FV) — letter bands
    IF pay_plan = 'FV' THEN
        RETURN CASE UPPER(grade)
            WHEN 'A' THEN 2    -- GS-1 to GS-4
            WHEN 'B' THEN 6    -- GS-5 to GS-8
            WHEN 'C' THEN 9    -- GS-9 to GS-10
            WHEN 'D' THEN 10   -- GS-9 to GS-12
            WHEN 'E' THEN 8    -- GS-5 to GS-8
            WHEN 'F' THEN 10   -- GS-9 to GS-12
            WHEN 'G' THEN 13   -- GS-13 to GS-14
            WHEN 'H' THEN 14   -- GS-14 to GS-15
            WHEN 'I' THEN 15   -- GS-15+
            WHEN 'J' THEN 15   -- SES equivalent
            WHEN 'K' THEN 15   -- SES equivalent
            WHEN 'L' THEN 15   -- Executive
            WHEN 'M' THEN 15   -- Executive
            ELSE NULL
        END;
    END IF;

    -- FAA Air Traffic (AT) — same as FV
    IF pay_plan = 'AT' THEN
        RETURN CASE UPPER(grade)
            WHEN 'A' THEN 2
            WHEN 'B' THEN 6
            WHEN 'C' THEN 9
            WHEN 'D' THEN 10
            WHEN 'E' THEN 8
            WHEN 'F' THEN 10
            WHEN 'G' THEN 13
            WHEN 'H' THEN 14
            WHEN 'I' THEN 15
            WHEN 'J' THEN 15
            WHEN 'K' THEN 15
            ELSE NULL
        END;
    END IF;

    -- TSA (SV) — letter bands
    IF pay_plan = 'SV' THEN
        RETURN CASE UPPER(grade)
            WHEN 'A' THEN 2    -- GS-1 to GS-3
            WHEN 'B' THEN 4    -- GS-4
            WHEN 'C' THEN 5    -- GS-5
            WHEN 'D' THEN 5    -- GS-5 to GS-6
            WHEN 'E' THEN 7    -- GS-7 to GS-8
            WHEN 'F' THEN 10   -- GS-9 to GS-11
            WHEN 'G' THEN 12   -- GS-12 to GS-13
            WHEN 'H' THEN 14   -- GS-14 to GS-15
            WHEN 'I' THEN 15   -- SES
            WHEN 'J' THEN 15   -- SES
            WHEN 'K' THEN 15   -- SES
            ELSE NULL
        END;
    END IF;

    -- TSA Executives (SW)
    IF pay_plan = 'SW' THEN
        RETURN 15; -- SES equivalent
    END IF;

    -- Federal Wage System — approximate crosswalk
    IF pay_plan IN ('WG','WL','WS','WB','WD','WK','WN','WY','WE',
                    'WJ','WM','WT','NA','NL','NS','NF','NV',
                    'XA','XC','XE','XF','XH') THEN
        RETURN CASE
            WHEN grade ~ '^\d+$' THEN
                CASE
                    WHEN grade::integer <= 5 THEN grade::integer
                    WHEN grade::integer <= 8 THEN grade::integer - 1
                    WHEN grade::integer <= 11 THEN grade::integer - 1
                    WHEN grade::integer <= 15 THEN grade::integer - 2
                    ELSE NULL
                END
            ELSE NULL
        END;
    END IF;

    -- Foreign Service (FP) — reverse scale
    IF pay_plan IN ('FP','FE','FB') THEN
        RETURN CASE
            WHEN grade ~ '^\d+$' THEN
                CASE grade::integer
                    WHEN 9 THEN 3
                    WHEN 8 THEN 5
                    WHEN 7 THEN 7
                    WHEN 6 THEN 9
                    WHEN 5 THEN 11
                    WHEN 4 THEN 12
                    WHEN 3 THEN 13
                    WHEN 2 THEN 14
                    WHEN 1 THEN 15
                    ELSE NULL
                END
            ELSE NULL
        END;
    END IF;

    -- Catch-all: unmapped/non-GS pay plans have no GS-equivalent.
    -- Return NULL instead of echoing the plan's native grade as a GS grade.
    RETURN NULL;
END;
$_$;

-- [min, max] GS-equivalent band for a pay plan + grade; used by jobs_geo for
-- gs_min/gs_max. Same coverage and NULL policy as gs_equivalent().
CREATE OR REPLACE FUNCTION gs_equivalent_range(pay_plan text, grade text) RETURNS integer[]
    LANGUAGE plpgsql IMMUTABLE
    AS $_$
BEGIN
    IF grade IS NULL OR grade = '' THEN
        RETURN NULL;
    END IF;

    -- 1:1 plans — range is just the grade itself
    IF pay_plan IN ('GS','GL','GG','FG') THEN
        IF grade ~ '^\d+$' THEN
            RETURN ARRAY[grade::integer, grade::integer];
        END IF;
        RETURN NULL;
    END IF;

    -- NH, NM, NQ (Professional/Supervisory)
    IF pay_plan IN ('NH','NM','NQ') THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,11]  WHEN '02' THEN ARRAY[5,11]
            WHEN '3' THEN ARRAY[12,13] WHEN '03' THEN ARRAY[12,13]
            WHEN '4' THEN ARRAY[14,15] WHEN '04' THEN ARRAY[14,15]
            ELSE NULL
        END;
    END IF;

    -- NJ (Technical)
    IF pay_plan = 'NJ' THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,10]  WHEN '02' THEN ARRAY[5,10]
            WHEN '3' THEN ARRAY[11,13] WHEN '03' THEN ARRAY[11,13]
            WHEN '4' THEN ARRAY[14,15] WHEN '04' THEN ARRAY[14,15]
            ELSE NULL
        END;
    END IF;

    -- NK (Admin Support)
    IF pay_plan = 'NK' THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,7]   WHEN '02' THEN ARRAY[5,7]
            WHEN '3' THEN ARRAY[8,10]  WHEN '03' THEN ARRAY[8,10]
            WHEN '4' THEN ARRAY[11,13] WHEN '04' THEN ARRAY[11,13]
            ELSE NULL
        END;
    END IF;

    -- ND, DP (Scientists & Engineers)
    IF pay_plan IN ('ND','DP') THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,11]  WHEN '02' THEN ARRAY[5,11]
            WHEN '3' THEN ARRAY[12,13] WHEN '03' THEN ARRAY[12,13]
            WHEN '4' THEN ARRAY[14,15] WHEN '04' THEN ARRAY[14,15]
            WHEN '5' THEN ARRAY[15,15] WHEN '05' THEN ARRAY[15,15]
            WHEN '6' THEN ARRAY[15,15] WHEN '06' THEN ARRAY[15,15]
            ELSE NULL
        END;
    END IF;

    -- NT, DS (Technical)
    IF pay_plan IN ('NT','DS') THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,10]  WHEN '02' THEN ARRAY[5,10]
            WHEN '3' THEN ARRAY[11,13] WHEN '03' THEN ARRAY[11,13]
            WHEN '4' THEN ARRAY[14,15] WHEN '04' THEN ARRAY[14,15]
            WHEN '5' THEN ARRAY[15,15] WHEN '05' THEN ARRAY[15,15]
            WHEN '6' THEN ARRAY[15,15] WHEN '06' THEN ARRAY[15,15]
            ELSE NULL
        END;
    END IF;

    -- NO, DB (Admin)
    IF pay_plan IN ('NO','DB') THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,7]   WHEN '02' THEN ARRAY[5,7]
            WHEN '3' THEN ARRAY[8,10]  WHEN '03' THEN ARRAY[8,10]
            WHEN '4' THEN ARRAY[11,13] WHEN '04' THEN ARRAY[11,13]
            WHEN '5' THEN ARRAY[15,15] WHEN '05' THEN ARRAY[15,15]
            ELSE NULL
        END;
    END IF;

    -- DA (Admin Professional)
    IF pay_plan = 'DA' THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,11]  WHEN '02' THEN ARRAY[5,11]
            WHEN '3' THEN ARRAY[12,13] WHEN '03' THEN ARRAY[12,13]
            WHEN '4' THEN ARRAY[14,15] WHEN '04' THEN ARRAY[14,15]
            WHEN '5' THEN ARRAY[15,15] WHEN '05' THEN ARRAY[15,15]
            ELSE NULL
        END;
    END IF;

    -- ZP, ZA (NIST Professional/Admin)
    IF pay_plan IN ('ZP','ZA') THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,11]  WHEN '02' THEN ARRAY[5,11]
            WHEN '3' THEN ARRAY[12,13] WHEN '03' THEN ARRAY[12,13]
            WHEN '4' THEN ARRAY[14,15] WHEN '04' THEN ARRAY[14,15]
            WHEN '5' THEN ARRAY[15,15] WHEN '05' THEN ARRAY[15,15]
            ELSE NULL
        END;
    END IF;

    -- ZT, ZS (NIST Technician/Support)
    IF pay_plan IN ('ZT','ZS') THEN
        RETURN CASE grade
            WHEN '1' THEN ARRAY[1,4]   WHEN '01' THEN ARRAY[1,4]
            WHEN '2' THEN ARRAY[5,8]   WHEN '02' THEN ARRAY[5,8]
            WHEN '3' THEN ARRAY[9,11]  WHEN '03' THEN ARRAY[9,11]
            WHEN '4' THEN ARRAY[12,13] WHEN '04' THEN ARRAY[12,13]
            ELSE NULL
        END;
    END IF;

    -- FV (FAA)
    IF pay_plan IN ('FV','AT') THEN
        RETURN CASE UPPER(grade)
            WHEN 'A' THEN ARRAY[1,4]
            WHEN 'B' THEN ARRAY[5,8]
            WHEN 'C' THEN ARRAY[9,10]
            WHEN 'D' THEN ARRAY[9,12]
            WHEN 'E' THEN ARRAY[5,8]
            WHEN 'F' THEN ARRAY[9,12]
            WHEN 'G' THEN ARRAY[13,14]
            WHEN 'H' THEN ARRAY[14,15]
            WHEN 'I' THEN ARRAY[15,15]
            WHEN 'J' THEN ARRAY[15,15]
            WHEN 'K' THEN ARRAY[15,15]
            WHEN 'L' THEN ARRAY[15,15]
            WHEN 'M' THEN ARRAY[15,15]
            ELSE NULL
        END;
    END IF;

    -- SV (TSA)
    IF pay_plan = 'SV' THEN
        RETURN CASE UPPER(grade)
            WHEN 'A' THEN ARRAY[1,3]
            WHEN 'B' THEN ARRAY[4,4]
            WHEN 'C' THEN ARRAY[5,5]
            WHEN 'D' THEN ARRAY[5,6]
            WHEN 'E' THEN ARRAY[7,8]
            WHEN 'F' THEN ARRAY[9,11]
            WHEN 'G' THEN ARRAY[12,13]
            WHEN 'H' THEN ARRAY[14,15]
            WHEN 'I' THEN ARRAY[15,15]
            WHEN 'J' THEN ARRAY[15,15]
            WHEN 'K' THEN ARRAY[15,15]
            ELSE NULL
        END;
    END IF;

    IF pay_plan = 'SW' THEN
        RETURN ARRAY[15,15];
    END IF;

    -- FWS approximate
    IF pay_plan IN ('WG','WL','WS','WB','WD','WK','WN','WY','WE',
                    'WJ','WM','WT','NA','NL','NS','NF','NV',
                    'XA','XC','XE','XF','XH') THEN
        IF grade ~ '^\d+$' THEN
            RETURN CASE
                WHEN grade::integer <= 5 THEN ARRAY[grade::integer, grade::integer]
                WHEN grade::integer <= 8 THEN ARRAY[grade::integer - 1, grade::integer]
                WHEN grade::integer <= 11 THEN ARRAY[grade::integer - 1, grade::integer]
                WHEN grade::integer <= 15 THEN ARRAY[grade::integer - 2, grade::integer - 1]
                ELSE NULL
            END;
        END IF;
        RETURN NULL;
    END IF;

    -- FP (Foreign Service — reverse scale)
    IF pay_plan IN ('FP','FE','FB') THEN
        IF grade ~ '^\d+$' THEN
            RETURN CASE grade::integer
                WHEN 9 THEN ARRAY[3,4]
                WHEN 8 THEN ARRAY[5,5]
                WHEN 7 THEN ARRAY[7,7]
                WHEN 6 THEN ARRAY[9,9]
                WHEN 5 THEN ARRAY[11,11]
                WHEN 4 THEN ARRAY[12,12]
                WHEN 3 THEN ARRAY[13,13]
                WHEN 2 THEN ARRAY[14,14]
                WHEN 1 THEN ARRAY[15,15]
                ELSE NULL
            END;
        END IF;
        RETURN NULL;
    END IF;

    -- Catch-all: unmapped/non-GS pay plans have no GS-equivalent.
    -- Return NULL instead of echoing the plan's native grade as a GS grade.
    RETURN NULL;
END;
$_$;

-- ============================================================
-- Materialized view: jobs_geo (one row per job per location)
-- ============================================================
--
-- Definition verbatim from the live database. The nesting is the migration
-- history preserved: each migration wrapped the previous definition as a
-- subquery instead of restating it. Reading inside-out:
--   _src  — base parse of jobs_raw: one row per (position_id, location),
--           columns extracted from the JSON payload; zero coordinates pinned
--           to Washington DC (38.89, -77.03); spatial LEFT JOIN us_counties
--           gives fips, then LEFT JOIN locality_areas gives locality_area.
--           WHERE excludes: locations without coordinates; medical/health
--           series (0182, 0602-0799 list); "agency wide" orgs whose posting
--           window exceeds 180 days; national guard orgs (exclusions were
--           applied live first, then captured in migration 06's restatement)
--   _open — recency: last_seen within 2 days (migration 03)
--   jg    — application still open: close_date empty or >= today (migration 04)
--   outer — adds is_evergreen flag: >= 50 duty locations OR posting window
--           >= 300 days; rows are flagged, not dropped (migration 06)

CREATE MATERIALIZED VIEW jobs_geo AS
 SELECT position_id,
    location_name,
    city_name,
    state,
    country,
    lat,
    lon,
    geom,
    title,
    org,
    department,
    min_salary,
    max_salary,
    rate_interval,
    clearance,
    pay_plan,
    low_grade,
    high_grade,
    gs_min,
    gs_max,
    remote,
    telework,
    close_date,
    series_code,
    series_name,
    fips,
    locality_area,
    first_seen,
    last_seen,
    (EXISTS ( SELECT 1
           FROM public.jobs_raw r
          WHERE ((r.position_id = jg.position_id) AND ((jsonb_array_length(((r.data -> 'MatchedObjectDescriptor'::text) -> 'PositionLocation'::text)) >= 50) OR (((((r.data -> 'MatchedObjectDescriptor'::text) ->> 'ApplicationCloseDate'::text))::timestamp with time zone - (((r.data -> 'MatchedObjectDescriptor'::text) ->> 'PublicationStartDate'::text))::timestamp with time zone) >= '300 days'::interval))))) AS is_evergreen
   FROM ( SELECT _open.position_id,
            _open.location_name,
            _open.city_name,
            _open.state,
            _open.country,
            _open.lat,
            _open.lon,
            _open.geom,
            _open.title,
            _open.org,
            _open.department,
            _open.min_salary,
            _open.max_salary,
            _open.rate_interval,
            _open.clearance,
            _open.pay_plan,
            _open.low_grade,
            _open.high_grade,
            _open.gs_min,
            _open.gs_max,
            _open.remote,
            _open.telework,
            _open.close_date,
            _open.series_code,
            _open.series_name,
            _open.fips,
            _open.locality_area,
            _open.first_seen,
            _open.last_seen
           FROM ( SELECT _src.position_id,
                    _src.location_name,
                    _src.city_name,
                    _src.state,
                    _src.country,
                    _src.lat,
                    _src.lon,
                    _src.geom,
                    _src.title,
                    _src.org,
                    _src.department,
                    _src.min_salary,
                    _src.max_salary,
                    _src.rate_interval,
                    _src.clearance,
                    _src.pay_plan,
                    _src.low_grade,
                    _src.high_grade,
                    _src.gs_min,
                    _src.gs_max,
                    _src.remote,
                    _src.telework,
                    _src.close_date,
                    _src.series_code,
                    _src.series_name,
                    _src.fips,
                    _src.locality_area,
                    _src.first_seen,
                    _src.last_seen
                   FROM ( SELECT DISTINCT ON (j.position_id, (loc.value ->> 'LocationName'::text)) j.position_id,
                            (loc.value ->> 'LocationName'::text) AS location_name,
                            (loc.value ->> 'CityName'::text) AS city_name,
                            (loc.value ->> 'CountrySubDivisionCode'::text) AS state,
                            (loc.value ->> 'CountryCode'::text) AS country,
                                CASE
                                    WHEN (((loc.value ->> 'Latitude'::text))::numeric = (0)::numeric) THEN 38.89
                                    ELSE ((loc.value ->> 'Latitude'::text))::numeric
                                END AS lat,
                                CASE
                                    WHEN (((loc.value ->> 'Longitude'::text))::numeric = (0)::numeric) THEN '-77.03'::numeric
                                    ELSE ((loc.value ->> 'Longitude'::text))::numeric
                                END AS lon,
                            public.st_setsrid(public.st_makepoint(
                                CASE
                                    WHEN (((loc.value ->> 'Longitude'::text))::numeric = (0)::numeric) THEN (- (77.03)::double precision)
                                    ELSE ((loc.value ->> 'Longitude'::text))::double precision
                                END,
                                CASE
                                    WHEN (((loc.value ->> 'Latitude'::text))::numeric = (0)::numeric) THEN (38.89)::double precision
                                    ELSE ((loc.value ->> 'Latitude'::text))::double precision
                                END), 4326) AS geom,
                            ((j.data -> 'MatchedObjectDescriptor'::text) ->> 'PositionTitle'::text) AS title,
                            ((j.data -> 'MatchedObjectDescriptor'::text) ->> 'OrganizationName'::text) AS org,
                            ((j.data -> 'MatchedObjectDescriptor'::text) ->> 'DepartmentName'::text) AS department,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionRemuneration'::text) -> 0) ->> 'MinimumRange'::text) AS min_salary,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionRemuneration'::text) -> 0) ->> 'MaximumRange'::text) AS max_salary,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionRemuneration'::text) -> 0) ->> 'RateIntervalCode'::text) AS rate_interval,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'SecurityClearance'::text) AS clearance,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobGrade'::text) -> 0) ->> 'Code'::text) AS pay_plan,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'LowGrade'::text) AS low_grade,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'HighGrade'::text) AS high_grade,
                            (public.gs_equivalent_range(((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobGrade'::text) -> 0) ->> 'Code'::text), ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'LowGrade'::text)))[1] AS gs_min,
                            (public.gs_equivalent_range(((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobGrade'::text) -> 0) ->> 'Code'::text), ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'HighGrade'::text)))[2] AS gs_max,
                            COALESCE((((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'RemoteIndicator'::text) = 'true'::text), false) AS remote,
                            COALESCE((((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'TeleworkEligible'::text) = 'true'::text), false) AS telework,
                            ((j.data -> 'MatchedObjectDescriptor'::text) ->> 'ApplicationCloseDate'::text) AS close_date,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobCategory'::text) -> 0) ->> 'Code'::text) AS series_code,
                            ((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobCategory'::text) -> 0) ->> 'Name'::text) AS series_name,
                            c.fips,
                            la.locality AS locality_area,
                            j.first_seen,
                            j.last_seen
                           FROM public.jobs_raw j,
                            ((LATERAL jsonb_array_elements(((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionLocation'::text)) loc(value)
                             LEFT JOIN public.us_counties c ON (public.st_within(public.st_setsrid(public.st_makepoint(
                                CASE
                                    WHEN (((loc.value ->> 'Longitude'::text))::numeric = (0)::numeric) THEN (- (77.03)::double precision)
                                    ELSE ((loc.value ->> 'Longitude'::text))::double precision
                                END,
                                CASE
                                    WHEN (((loc.value ->> 'Latitude'::text))::numeric = (0)::numeric) THEN (38.89)::double precision
                                    ELSE ((loc.value ->> 'Latitude'::text))::double precision
                                END), 4326), c.geom)))
                             LEFT JOIN public.locality_areas la ON ((c.fips = la.fips)))
                          WHERE (((loc.value ->> 'Latitude'::text) IS NOT NULL) AND ((loc.value ->> 'Longitude'::text) IS NOT NULL) AND (COALESCE(((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobCategory'::text) -> 0) ->> 'Code'::text), ''::text) <> ALL (ARRAY['0182'::text, '0602'::text, '0603'::text, '0610'::text, '0620'::text, '0630'::text, '0631'::text, '0633'::text, '0635'::text, '0636'::text, '0638'::text, '0640'::text, '0642'::text, '0645'::text, '0646'::text, '0647'::text, '0648'::text, '0649'::text, '0651'::text, '0660'::text, '0661'::text, '0662'::text, '0665'::text, '0667'::text, '0668'::text, '0672'::text, '0679'::text, '0680'::text, '0681'::text, '0682'::text, '0683'::text, '0699'::text, '0701'::text, '0799'::text])) AND (NOT ((((j.data -> 'MatchedObjectDescriptor'::text) ->> 'OrganizationName'::text) ~~* '%agency wide%'::text) AND (((((j.data -> 'MatchedObjectDescriptor'::text) ->> 'ApplicationCloseDate'::text))::timestamp without time zone - (((j.data -> 'MatchedObjectDescriptor'::text) ->> 'PublicationStartDate'::text))::timestamp without time zone) > '180 days'::interval))) AND (((j.data -> 'MatchedObjectDescriptor'::text) ->> 'OrganizationName'::text) !~~* '%national guard%'::text))) _src
                  WHERE (_src.last_seen >= (now() - '2 days'::interval))) _open
          WHERE ((NULLIF(_open.close_date, ''::text) IS NULL) OR ((_open.close_date)::date >= CURRENT_DATE))) jg
  WITH NO DATA;

-- First population must be a plain refresh; CONCURRENTLY only works once the
-- view has data (and needs idx_jobs_geo_pk below). Routine refreshes go
-- through refresh_jobs_geo().
--   REFRESH MATERIALIZED VIEW jobs_geo;

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
CREATE INDEX idx_jobs_geo_grade ON jobs_geo(gs_min, gs_max);
CREATE INDEX idx_jobs_geo_salary ON jobs_geo(min_salary, max_salary);
CREATE INDEX idx_jobs_geo_series ON jobs_geo(series_code);
CREATE INDEX idx_jobs_geo_fips ON jobs_geo(fips);
CREATE INDEX idx_jobs_geo_locality ON jobs_geo(locality_area);
CREATE INDEX idx_jobs_geo_evergreen ON jobs_geo(is_evergreen);

-- ============================================================
-- Refresh function (SECURITY DEFINER so the collector can refresh
-- the view without owning it; migration 02)
-- ============================================================

CREATE OR REPLACE FUNCTION refresh_jobs_geo() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY jobs_geo;
    UPDATE refresh_log SET last_refresh = NOW() WHERE id = 1;
END;
$$;

-- ============================================================
-- Roles and grants
-- ============================================================
--
-- Prerequisite (cluster-level, run as a superuser; choose real passwords):
--   CREATE ROLE usajobs_web LOGIN PASSWORD '<password>';
--   CREATE ROLE usajobs_collector LOGIN PASSWORD '<password>';
--
-- usajobs_web       — read-only role for the FastAPI app
-- usajobs_collector — writer role for collect.py
--
-- Note: locality_areas intentionally has no grants; the web role reads
-- locality data through the joined locality_area column of jobs_geo.

GRANT CONNECT ON DATABASE usajobs TO usajobs_web;
GRANT CONNECT ON DATABASE usajobs TO usajobs_collector;

GRANT USAGE ON SCHEMA public TO usajobs_web;
GRANT USAGE ON SCHEMA public TO usajobs_collector;

GRANT SELECT ON jobs_raw TO usajobs_web;
GRANT SELECT ON jobs_geo TO usajobs_web;
GRANT SELECT ON us_counties TO usajobs_web;
GRANT SELECT ON refresh_log TO usajobs_web;
GRANT SELECT ON sighting_returns TO usajobs_web;

GRANT SELECT, INSERT, UPDATE ON jobs_raw TO usajobs_collector;
GRANT SELECT, INSERT, UPDATE ON jobs_history TO usajobs_collector;
GRANT SELECT, USAGE ON SEQUENCE jobs_history_id_seq TO usajobs_collector;
GRANT SELECT, INSERT ON sighting_returns TO usajobs_collector;
GRANT SELECT, USAGE ON SEQUENCE sighting_returns_id_seq TO usajobs_collector;
GRANT SELECT, UPDATE ON refresh_log TO usajobs_collector;
GRANT ALL ON jobs_geo TO usajobs_collector;

REVOKE ALL ON FUNCTION refresh_jobs_geo() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_jobs_geo() TO usajobs_collector;

-- ============================================================
-- commercial schema (migration 08) — personal-use commercial cleared-jobs
-- layer, kept apart from the USAJobs tables above. Tables are created empty;
-- cj_collect.py populates them. Collected data is never republished.
-- ============================================================

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


-- ============================================================
-- commercial geo + indexes (migrations 09, 10, 11)
-- Appended snapshot of the post-migration-08 commercial changes: the
-- active-set index, the GeoNames gazetteer + job_locations table (loaded
-- by scripts/load_geonames.py and cj_collect.py), and the materialized
-- OPM locality_area used by exclude_ncr.
-- ============================================================

-- from migrations/09_commercial_indexes.sql
CREATE INDEX IF NOT EXISTS idx_commercial_jobs_active_posted
    ON commercial.jobs_raw ((data->>'datePosted') DESC NULLS LAST, ext_id)
    WHERE source = 'clearancejobs' AND data IS NOT NULL AND consecutive_misses = 0;

-- from migrations/10_commercial_geo.sql
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

-- from migrations/11_commercial_locality.sql
ALTER TABLE commercial.job_locations
    ADD COLUMN IF NOT EXISTS county_fips    TEXT,
    ADD COLUMN IF NOT EXISTS locality_area  TEXT;

CREATE INDEX IF NOT EXISTS idx_commercial_job_locations_locality
    ON commercial.job_locations(locality_area);

GRANT SELECT ON public.us_counties, public.locality_areas TO usajobs_collector;
