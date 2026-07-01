-- Migration 06: flag nationwide / open-continuous announcements in jobs_geo
--
-- Adds an is_evergreen boolean rather than dropping rows, so the web UI can toggle
-- them with a checkbox. A posting is evergreen when it has >= 50 duty locations
-- (nationwide; real billets list 1-20, registers list 69-1176) OR an announced
-- window (close - publication) >= 300 days (open-continuous; real postings incl.
-- 6-month DHA windows stay <= 183 days). Deliberately NOT keyed on TotalOpenings
-- ='Many' or a 180-day window, which would mis-flag legit DHA bulk-hire billets.
--
-- Usage: psql -h localhost -U usajobs -d usajobs -f migrations/06_jobs_geo_exclude_evergreen.sql

BEGIN;
DROP MATERIALIZED VIEW jobs_geo;
CREATE MATERIALIZED VIEW jobs_geo AS
SELECT jg.*,
    (EXISTS (
        SELECT 1 FROM jobs_raw r
        WHERE r.position_id = jg.position_id
          AND ( jsonb_array_length(r.data->'MatchedObjectDescriptor'->'PositionLocation') >= 50
             OR (r.data->'MatchedObjectDescriptor'->>'ApplicationCloseDate')::timestamptz
              - (r.data->'MatchedObjectDescriptor'->>'PublicationStartDate')::timestamptz >= interval '300 days' )
    )) AS is_evergreen
FROM (
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
    last_seen
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
                    loc.value ->> 'LocationName'::text AS location_name,
                    loc.value ->> 'CityName'::text AS city_name,
                    loc.value ->> 'CountrySubDivisionCode'::text AS state,
                    loc.value ->> 'CountryCode'::text AS country,
                        CASE
                            WHEN ((loc.value ->> 'Latitude'::text)::numeric) = 0::numeric THEN 38.89
                            ELSE (loc.value ->> 'Latitude'::text)::numeric
                        END AS lat,
                        CASE
                            WHEN ((loc.value ->> 'Longitude'::text)::numeric) = 0::numeric THEN '-77.03'::numeric
                            ELSE (loc.value ->> 'Longitude'::text)::numeric
                        END AS lon,
                    st_setsrid(st_makepoint(
                        CASE
                            WHEN ((loc.value ->> 'Longitude'::text)::numeric) = 0::numeric THEN - 77.03::double precision
                            ELSE (loc.value ->> 'Longitude'::text)::double precision
                        END,
                        CASE
                            WHEN ((loc.value ->> 'Latitude'::text)::numeric) = 0::numeric THEN 38.89::double precision
                            ELSE (loc.value ->> 'Latitude'::text)::double precision
                        END), 4326) AS geom,
                    (j.data -> 'MatchedObjectDescriptor'::text) ->> 'PositionTitle'::text AS title,
                    (j.data -> 'MatchedObjectDescriptor'::text) ->> 'OrganizationName'::text AS org,
                    (j.data -> 'MatchedObjectDescriptor'::text) ->> 'DepartmentName'::text AS department,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionRemuneration'::text) -> 0) ->> 'MinimumRange'::text AS min_salary,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionRemuneration'::text) -> 0) ->> 'MaximumRange'::text AS max_salary,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionRemuneration'::text) -> 0) ->> 'RateIntervalCode'::text AS rate_interval,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'SecurityClearance'::text AS clearance,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobGrade'::text) -> 0) ->> 'Code'::text AS pay_plan,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'LowGrade'::text AS low_grade,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'HighGrade'::text AS high_grade,
                    (gs_equivalent_range((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobGrade'::text) -> 0) ->> 'Code'::text, (((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'LowGrade'::text))[1] AS gs_min,
                    (gs_equivalent_range((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobGrade'::text) -> 0) ->> 'Code'::text, (((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'HighGrade'::text))[2] AS gs_max,
                    COALESCE(((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'RemoteIndicator'::text) = 'true'::text, false) AS remote,
                    COALESCE(((((j.data -> 'MatchedObjectDescriptor'::text) -> 'UserArea'::text) -> 'Details'::text) ->> 'TeleworkEligible'::text) = 'true'::text, false) AS telework,
                    (j.data -> 'MatchedObjectDescriptor'::text) ->> 'ApplicationCloseDate'::text AS close_date,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobCategory'::text) -> 0) ->> 'Code'::text AS series_code,
                    (((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobCategory'::text) -> 0) ->> 'Name'::text AS series_name,
                    c.fips,
                    la.locality AS locality_area,
                    j.first_seen,
                    j.last_seen
                   FROM jobs_raw j,
                    LATERAL jsonb_array_elements((j.data -> 'MatchedObjectDescriptor'::text) -> 'PositionLocation'::text) loc(value)
                     LEFT JOIN us_counties c ON st_within(st_setsrid(st_makepoint(
                        CASE
                            WHEN ((loc.value ->> 'Longitude'::text)::numeric) = 0::numeric THEN - 77.03::double precision
                            ELSE (loc.value ->> 'Longitude'::text)::double precision
                        END,
                        CASE
                            WHEN ((loc.value ->> 'Latitude'::text)::numeric) = 0::numeric THEN 38.89::double precision
                            ELSE (loc.value ->> 'Latitude'::text)::double precision
                        END), 4326), c.geom)
                     LEFT JOIN locality_areas la ON c.fips = la.fips
                  WHERE (loc.value ->> 'Latitude'::text) IS NOT NULL AND (loc.value ->> 'Longitude'::text) IS NOT NULL AND (COALESCE((((j.data -> 'MatchedObjectDescriptor'::text) -> 'JobCategory'::text) -> 0) ->> 'Code'::text, ''::text) <> ALL (ARRAY['0182'::text, '0602'::text, '0603'::text, '0610'::text, '0620'::text, '0630'::text, '0631'::text, '0633'::text, '0635'::text, '0636'::text, '0638'::text, '0640'::text, '0642'::text, '0645'::text, '0646'::text, '0647'::text, '0648'::text, '0649'::text, '0651'::text, '0660'::text, '0661'::text, '0662'::text, '0665'::text, '0667'::text, '0668'::text, '0672'::text, '0679'::text, '0680'::text, '0681'::text, '0682'::text, '0683'::text, '0699'::text, '0701'::text, '0799'::text])) AND NOT (((j.data -> 'MatchedObjectDescriptor'::text) ->> 'OrganizationName'::text) ~~* '%agency wide%'::text AND ((((j.data -> 'MatchedObjectDescriptor'::text) ->> 'ApplicationCloseDate'::text)::timestamp without time zone) - (((j.data -> 'MatchedObjectDescriptor'::text) ->> 'PublicationStartDate'::text)::timestamp without time zone)) > '180 days'::interval) AND ((j.data -> 'MatchedObjectDescriptor'::text) ->> 'OrganizationName'::text) !~~* '%national guard%'::text) _src
          WHERE _src.last_seen >= (now() - '2 days'::interval)) _open
  WHERE NULLIF(close_date, ''::text) IS NULL OR close_date::date >= CURRENT_DATE
) jg;

CREATE INDEX idx_jobs_geo_clearance ON public.jobs_geo USING btree (clearance);
CREATE INDEX idx_jobs_geo_country ON public.jobs_geo USING btree (country);
CREATE INDEX idx_jobs_geo_fips ON public.jobs_geo USING btree (fips);
CREATE INDEX idx_jobs_geo_geom ON public.jobs_geo USING gist (geom);
CREATE INDEX idx_jobs_geo_grade ON public.jobs_geo USING btree (gs_min, gs_max);
CREATE INDEX idx_jobs_geo_locality ON public.jobs_geo USING btree (locality_area);
CREATE INDEX idx_jobs_geo_org ON public.jobs_geo USING btree (org);
CREATE INDEX idx_jobs_geo_salary ON public.jobs_geo USING btree (min_salary, max_salary);
CREATE INDEX idx_jobs_geo_series ON public.jobs_geo USING btree (series_code);
CREATE INDEX idx_jobs_geo_state ON public.jobs_geo USING btree (state);
CREATE INDEX idx_jobs_geo_title_fts ON public.jobs_geo USING gin (to_tsvector('english'::regconfig, title));
CREATE UNIQUE INDEX idx_jobs_geo_pk ON public.jobs_geo USING btree (position_id, location_name);
CREATE INDEX idx_jobs_geo_evergreen ON jobs_geo (is_evergreen);

GRANT SELECT ON jobs_geo TO usajobs_web;
GRANT ALL ON jobs_geo TO usajobs_collector;
COMMIT;
