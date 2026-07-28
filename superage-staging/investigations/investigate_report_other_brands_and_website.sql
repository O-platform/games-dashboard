-- =============================================================================
-- Interpreting the Slack report's "other brands" (Week Jul 18–24, 2026)
--   + which chain level the Website subs come from
-- Window: date_joined in [2026-07-18, 2026-07-25), state = 'active'.
-- REFRESH the MV first so the corrected canon (quiz pass-through) is applied:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_subscriber_acquisition;
-- =============================================================================


-- 1. What the DEPLOYED report actually put in "other brands".
--    used_bucket = deployed rule (Meta = plain facebook/ig only). For every sub
--    it bucketed as other_brands, show what it REALLY is (corrected source_label).
WITH c AS (
    SELECT
        source_label,
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
        END AS used_bucket
    FROM superage.mv_subscriber_acquisition
    WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
      AND LOWER(TRIM(COALESCE(state,''))) = 'active'
)
SELECT
    COALESCE(source_label, 'Organic') AS actually_is,
    COUNT(*)                          AS subs
FROM c
WHERE used_bucket = 'other_brands'
GROUP BY 1
ORDER BY 2 DESC;


-- 2. WEBSITE subs — which chain LEVEL earns them the 'Website' label?
--    (L2 is Meta-only, so Website can only come from L1/L3/L4/L5.)
SELECT
    CASE
        WHEN superage.canon_source(acquisition_utm_source) = 'Website'
            THEN 'L1 acquisition_utm_source: ' || LOWER(TRIM(acquisition_utm_source))
        WHEN NULLIF(superage.canon_source(sub_source), 'Taboola') = 'Website'
            THEN 'L3 sub_source: ' || LOWER(TRIM(sub_source))
        WHEN NULLIF(superage.canon_source(source), 'Taboola') = 'Website'
            THEN 'L4 source: ' || LOWER(TRIM(source))
        WHEN NULLIF(superage.canon_source(utm_source), 'Taboola') = 'Website'
            THEN 'L5 utm_source: ' || LOWER(TRIM(utm_source))
        ELSE '(unresolved)'
    END AS website_driver,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
GROUP BY 1
ORDER BY subs DESC;


-- 3. Website subs — full raw-field picture (what else is on them?)
SELECT
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
GROUP BY 1,2,3,4
ORDER BY subs DESC
LIMIT 30;
