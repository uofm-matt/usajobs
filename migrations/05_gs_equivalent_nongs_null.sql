-- Migration 04: non-GS pay plans have no GS-equivalent grade
--
-- Only GS/GL/GG/FG use the General Schedule 1-15 grade scale. The original
-- 1:1 list wrongly included ~34 non-GS plans (AD, SL/ES/ST, SK, CG, the VA
-- Title-38 family VN/VM/VH/VC/VP, ...) and echoed their native grade as a GS
-- grade (AD-23 -> GS-23, which doesn't exist); the catch-all did the same for
-- unmapped plans (CL-31, CN-81), and the Federal Wage System block passed
-- grades above 15 (WS-16) straight through. Now: GS/GL/GG/FG pass 1:1, the
-- band crosswalks (NH/ND/ZP/FV/SV/FWS<=15/FP) stand, and everything else --
-- unmapped plans and FWS grades >15 -- returns NULL. Native grade still lives
-- in low_grade/high_grade/pay_plan; only the GS-equivalent columns go NULL.
--
-- Usage: psql -h localhost -U usajobs -d usajobs -f migrations/05_gs_equivalent_nongs_null.sql
-- Then: SELECT refresh_jobs_geo();

-- ============================================================
-- Function: convert pay_plan + grade to GS equivalent
-- ============================================================

CREATE OR REPLACE FUNCTION gs_equivalent(pay_plan TEXT, grade TEXT)
RETURNS INTEGER AS $$
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
$$ LANGUAGE plpgsql IMMUTABLE;


-- ============================================================
-- GS range function: returns the low and high GS equivalents
-- for a pay band (e.g., NH-04 → 14, 15)
-- ============================================================

CREATE OR REPLACE FUNCTION gs_equivalent_range(pay_plan TEXT, grade TEXT)
RETURNS INTEGER[] AS $$
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
$$ LANGUAGE plpgsql IMMUTABLE;
