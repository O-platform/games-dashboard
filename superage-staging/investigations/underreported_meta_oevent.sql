-- =============================================================================
-- The UNDER-REPORTED Meta — subs the deployed rule called 'other_brands' but
-- the corrected chain calls 'Meta' — split by o_event (longevity vs fitness).
-- Week Jul 18–24, 2026, active. Refresh MV first.
-- =============================================================================
WITH c AS (
    SELECT
        o_event,
        source_label,
        CASE                              -- deployed/production bucket
            WHEN LOWER(TRIM(acquisition_utm_source)) LIKE 'taboola%%'                       THEN 'taboola'
            WHEN LOWER(TRIM(acquisition_utm_source)) IN ('facebook','meta','fb','ig')       THEN 'meta'
            WHEN COALESCE(TRIM(acquisition_utm_source),'')<>''
             AND LOWER(TRIM(acquisition_utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN LOWER(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)')))='meta'    THEN 'meta'
            WHEN LOWER(TRIM(sub_source)) IN ('facebook','meta','fb','ig')                   THEN 'meta'
            WHEN COALESCE(TRIM(sub_source),'')<>''
             AND LOWER(TRIM(sub_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN LOWER(TRIM(source)) IN ('facebook','meta','fb','ig')                       THEN 'meta'
            WHEN COALESCE(TRIM(source),'')<>''
             AND LOWER(TRIM(source)) NOT IN ('none','null','(none)','(null)','-','n/a')     THEN 'other_brands'
            WHEN LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')                   THEN 'meta'
            WHEN COALESCE(TRIM(utm_source),'')<>''
             AND LOWER(TRIM(utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            ELSE 'organic_or_unknown'
        END AS used_bucket
    FROM superage.mv_subscriber_acquisition
    WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
      AND LOWER(TRIM(COALESCE(state,''))) = 'active'
)
SELECT
    COALESCE(NULLIF(TRIM(o_event),''),'(empty)') AS o_event,
    COUNT(*) AS underreported_meta_subs
FROM c
WHERE source_label = 'Meta' AND used_bucket = 'other_brands'
GROUP BY 1
ORDER BY underreported_meta_subs DESC;
