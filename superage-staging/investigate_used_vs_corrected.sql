-- =============================================================================
-- USED (deployed Friday report) vs CORRECTED — Week of Jul 18–24, 2026
-- Window: date_joined in [2026-07-18, 2026-07-25), state = 'active'.
--   NOTE the window is by date_joined, so weekend (Jul 25+) signups do NOT leak
--   in — re-running today (Mon) covers the same subs as the Fri report, except
--   for any late-arriving acquisition rows the MV picked up on its last refresh.
--   => Refresh the MV before trusting this:
--      REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_subscriber_acquisition;
--
-- USED rule      = the chain BEFORE the fix: Meta = plain facebook/ig/meta/fb
--                  only; campaign names (fitness_power_quiz, longevity_quiz…)
--                  NOT recognised, so they fall to other_brands. Taboola @ L1.
-- CORRECTED rule = mv.source_label (campaign names mapped to Meta + DISTINCT ON).
-- =============================================================================

WITH classified AS (
    SELECT
        email,
        -- ---------- USED (pre-fix) bucket ----------
        CASE
            WHEN LOWER(TRIM(acquisition_utm_source)) LIKE 'taboola%'                       THEN 'taboola'
            WHEN LOWER(TRIM(acquisition_utm_source)) IN ('facebook','meta','fb','ig')      THEN 'meta'
            WHEN COALESCE(TRIM(acquisition_utm_source),'') <> ''
             AND LOWER(TRIM(acquisition_utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN LOWER(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)'))) = 'meta' THEN 'meta'
            WHEN LOWER(TRIM(sub_source)) IN ('facebook','meta','fb','ig')                  THEN 'meta'
            WHEN COALESCE(TRIM(sub_source),'') <> ''
             AND LOWER(TRIM(sub_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN LOWER(TRIM(source)) IN ('facebook','meta','fb','ig')                      THEN 'meta'
            WHEN COALESCE(TRIM(source),'') <> ''
             AND LOWER(TRIM(source)) NOT IN ('none','null','(none)','(null)','-','n/a')    THEN 'other_brands'
            WHEN LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')                  THEN 'meta'
            WHEN COALESCE(TRIM(utm_source),'') <> ''
             AND LOWER(TRIM(utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN COALESCE(TRIM(acquisition_utm_source),'')='' AND COALESCE(TRIM(sub_source),'')=''
             AND COALESCE(TRIM(source),'')='' AND COALESCE(TRIM(utm_source),'')=''
             AND COALESCE(TRIM(url_variables),'')=''                                        THEN 'unknown'
            ELSE 'organic'
        END AS used_bucket,
        -- ---------- CORRECTED bucket (from the MV) ----------
        CASE
            WHEN source_label = 'Taboola' THEN 'taboola'
            WHEN source_label = 'Meta'    THEN 'meta'
            WHEN source_label = 'Organic' OR source_label IS NULL THEN
                CASE WHEN COALESCE(TRIM(acquisition_utm_source),'')='' AND COALESCE(TRIM(sub_source),'')=''
                      AND COALESCE(TRIM(source),'')='' AND COALESCE(TRIM(utm_source),'')=''
                      AND COALESCE(TRIM(url_variables),'')='' THEN 'unknown' ELSE 'organic' END
            ELSE 'other_brands'
        END AS corrected_bucket
    FROM superage.mv_subscriber_acquisition
    WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
      AND LOWER(TRIM(COALESCE(state,''))) = 'active'
)

-- Q1: side-by-side bucket totals (USED vs CORRECTED)
SELECT
    'meta'         AS bucket,
    COUNT(*) FILTER (WHERE used_bucket='meta')         AS used,
    COUNT(*) FILTER (WHERE corrected_bucket='meta')    AS corrected
FROM classified
UNION ALL SELECT 'other_brands',
    COUNT(*) FILTER (WHERE used_bucket='other_brands'),
    COUNT(*) FILTER (WHERE corrected_bucket='other_brands') FROM classified
UNION ALL SELECT 'taboola',
    COUNT(*) FILTER (WHERE used_bucket='taboola'),
    COUNT(*) FILTER (WHERE corrected_bucket='taboola') FROM classified
UNION ALL SELECT 'organic',
    COUNT(*) FILTER (WHERE used_bucket='organic'),
    COUNT(*) FILTER (WHERE corrected_bucket='organic') FROM classified
UNION ALL SELECT 'unknown',
    COUNT(*) FILTER (WHERE used_bucket='unknown'),
    COUNT(*) FILTER (WHERE corrected_bucket='unknown') FROM classified
ORDER BY corrected DESC;


-- Q2: the migration — where did each USED bucket go under CORRECTED?
-- (Run this block on its own; it reuses the same CTE.)
-- The 'other_brands (used) -> meta (corrected)' cell IS the ~500 that were
-- wrongly reported as "other brands".
WITH classified AS (
    SELECT
        CASE
            WHEN LOWER(TRIM(acquisition_utm_source)) LIKE 'taboola%'                       THEN 'taboola'
            WHEN LOWER(TRIM(acquisition_utm_source)) IN ('facebook','meta','fb','ig')      THEN 'meta'
            WHEN COALESCE(TRIM(acquisition_utm_source),'') <> ''
             AND LOWER(TRIM(acquisition_utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN LOWER(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)'))) = 'meta' THEN 'meta'
            WHEN LOWER(TRIM(sub_source)) IN ('facebook','meta','fb','ig')                  THEN 'meta'
            WHEN COALESCE(TRIM(sub_source),'') <> ''
             AND LOWER(TRIM(sub_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN LOWER(TRIM(source)) IN ('facebook','meta','fb','ig')                      THEN 'meta'
            WHEN COALESCE(TRIM(source),'') <> ''
             AND LOWER(TRIM(source)) NOT IN ('none','null','(none)','(null)','-','n/a')    THEN 'other_brands'
            WHEN LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')                  THEN 'meta'
            WHEN COALESCE(TRIM(utm_source),'') <> ''
             AND LOWER(TRIM(utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            ELSE 'organic_or_unknown'
        END AS used_bucket,
        CASE
            WHEN source_label = 'Taboola' THEN 'taboola'
            WHEN source_label = 'Meta'    THEN 'meta'
            WHEN source_label = 'Organic' OR source_label IS NULL THEN 'organic_or_unknown'
            ELSE 'other_brands'
        END AS corrected_bucket
    FROM superage.mv_subscriber_acquisition
    WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
      AND LOWER(TRIM(COALESCE(state,''))) = 'active'
)
SELECT used_bucket, corrected_bucket, COUNT(*) AS subs
FROM classified
GROUP BY 1,2
ORDER BY subs DESC;
