-- =============================================================================
-- WEBSITE ANSWER PACK — everything to answer "what is 'other brands' / Website"
-- Week Jul 18–24, 2026, active new subs.  Report sent Fri 2026-07-24.
-- Run REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_subscriber_acquisition; first.
-- Sections:
--   1) PRODUCTION Slack query (verbatim logic) — the "used" numbers
--   2) CORRECTED query (via MV source_label) — the right numbers
--   3) Website split: utm signal × quiz  (word-of-mouth vs actually-Meta)
--   4) Website subs that DO carry a utm_source (Meta or anything) — detail
--   5) Churn of the Website cohort since Friday, split by utm signal
-- =============================================================================


-- ============================================================================
-- 1) PRODUCTION SLACK QUERY  (exactly the deployed logic — Meta = facebook/ig
--    only; quiz names terminate at L3 as other_brands). This is what produced
--    the Friday buckets.
-- ============================================================================
WITH sa_acq AS (
    SELECT DISTINCT ON (LOWER(TRIM(email)))
        LOWER(TRIM(email)) AS email, acquisition_utm_source
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added', 'resubscribed')
    ORDER BY LOWER(TRIM(email)), acquisition_date DESC NULLS LAST
),
base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email))                                 AS email,
        LOWER(TRIM(COALESCE(sa.acquisition_utm_source, ''))) AS acq_utm,
        LOWER(TRIM(COALESCE(s.sub_source, '')))              AS sub_src,
        LOWER(TRIM(COALESCE(s.source, '')))                  AS src,
        LOWER(TRIM(COALESCE(s.utm_source, '')))              AS utm_src,
        LOWER(COALESCE(s.url_variables, ''))                 AS url_vars
    FROM superage."subscribers" s
    LEFT JOIN sa_acq sa ON sa.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-18' AND s.date_joined < '2026-07-25'
      AND s.email IS NOT NULL AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state, ''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
),
classified AS (
    SELECT email,
        CASE
            WHEN acq_utm LIKE 'taboola%%'                                                  THEN 'taboola'
            WHEN acq_utm IN ('facebook','meta','fb','ig')                                  THEN 'meta'
            WHEN acq_utm <> '' AND acq_utm NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN LOWER(TRIM(SUBSTRING(url_vars FROM 'utm_source=([^,&]+)'))) = 'meta'       THEN 'meta'
            WHEN sub_src IN ('facebook','meta','fb','ig')                                  THEN 'meta'
            WHEN sub_src <> '' AND sub_src NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN src IN ('facebook','meta','fb','ig')                                      THEN 'meta'
            WHEN src <> '' AND src NOT IN ('none','null','(none)','(null)','-','n/a')       THEN 'other_brands'
            WHEN utm_src IN ('facebook','meta','fb','ig')                                  THEN 'meta'
            WHEN utm_src <> '' AND utm_src NOT IN ('none','null','(none)','(null)','-','n/a') THEN 'other_brands'
            WHEN acq_utm='' AND sub_src='' AND src='' AND utm_src='' AND url_vars=''        THEN 'unknown'
            ELSE 'organic'
        END AS source_bucket
    FROM base
)
SELECT source_bucket, COUNT(*) AS subs
FROM classified GROUP BY 1 ORDER BY 2 DESC;


-- ============================================================================
-- 2) CORRECTED QUERY (via MV) — same 5 buckets, but source_label uses the fixed
--    canon (quiz names pass through to the real Meta signal).
-- ============================================================================
SELECT
    CASE
        WHEN source_label = 'Taboola' THEN 'taboola'
        WHEN source_label = 'Meta'    THEN 'meta'
        WHEN source_label = 'Organic' OR source_label IS NULL THEN
            CASE WHEN COALESCE(TRIM(acquisition_utm_source),'')='' AND COALESCE(TRIM(sub_source),'')=''
                  AND COALESCE(TRIM(source),'')='' AND COALESCE(TRIM(utm_source),'')=''
                  AND COALESCE(TRIM(url_variables),'')='' THEN 'unknown' ELSE 'organic' END
        ELSE 'other_brands'
    END AS source_bucket,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
GROUP BY 1 ORDER BY 2 DESC;


-- ============================================================================
-- 3) WEBSITE split: utm signal × quiz  — the core of the answer.
--    "website + longevity + no utm" = word-of-mouth taking the on-site quiz.
--    "website + longevity + utm=fb"  = actually came from a Meta ad.
-- ============================================================================
SELECT
    CASE
        WHEN LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')                     THEN 'utm = META (came from FB/IG)'
        WHEN COALESCE(TRIM(utm_source),'')='' OR LOWER(TRIM(utm_source)) IN ('none','null','(none)','(null)','-','n/a') THEN 'utm = empty (direct / word-of-mouth)'
        WHEN LOWER(TRIM(utm_source)) IN ('website','homepage','home','web','site','games_website') THEN 'utm = website (direct)'
        ELSE 'utm = other: ' || LOWER(TRIM(utm_source))
    END AS utm_signal,
    CASE
        WHEN LOWER(TRIM(sub_source)) IN ('longivity_quiz','longevity_quiz') THEN 'took longevity quiz'
        WHEN LOWER(TRIM(sub_source)) IN ('fitness_power_quiz','fitness_quiz') THEN 'took fitness quiz'
        WHEN COALESCE(TRIM(sub_source),'')='' THEN 'no sub_source'
        ELSE 'sub_source: ' || LOWER(TRIM(sub_source))
    END AS quiz_taken,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
GROUP BY 1,2
ORDER BY subs DESC;


-- ============================================================================
-- 4) WEBSITE subs that DO carry a utm_source (Meta or anything else) — detail.
--    These are the ones where source=website (L4) beat utm_source (L5).
-- ============================================================================
SELECT
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COALESCE(NULLIF(TRIM(o_event),''),'(none)')                AS o_event,
    COALESCE(NULLIF(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)')),''),'(no utm in url)') AS url_vars_utm,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
  AND COALESCE(TRIM(utm_source),'') <> ''
  AND LOWER(TRIM(utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a')
GROUP BY 1,2,3,4,5,6
ORDER BY subs DESC;


-- ============================================================================
-- 5) CHURN since Friday (2026-07-24) for the Website cohort, split by utm signal
-- ============================================================================
WITH cohort AS (
    SELECT
        mv.email,
        CASE
            WHEN LOWER(TRIM(mv.utm_source)) IN ('facebook','meta','fb','ig') THEN 'utm = META (came from FB/IG)'
            WHEN COALESCE(TRIM(mv.utm_source),'')='' OR LOWER(TRIM(mv.utm_source)) IN ('none','null','(none)','(null)','-','n/a') THEN 'utm = empty (direct / word-of-mouth)'
            ELSE 'utm = other'
        END AS utm_signal
    FROM superage.mv_subscriber_acquisition mv
    WHERE mv.date_joined >= '2026-07-18' AND mv.date_joined < '2026-07-25'
      AND LOWER(TRIM(COALESCE(mv.state,''))) = 'active'
      AND mv.source_label = 'Website'
)
SELECT
    COALESCE(c.utm_signal, '— TOTAL —')                              AS website_segment,
    COUNT(*)                                                         AS subs,
    COUNT(*) FILTER (WHERE LOWER(TRIM(s.state)) = 'active')          AS still_active,
    COUNT(*) FILTER (WHERE LOWER(TRIM(s.state)) = 'unsubscribed')    AS unsubscribed_now,
    COUNT(*) FILTER (WHERE s.date_unsubscribed::date >= '2026-07-24') AS unsubbed_since_report
FROM cohort c
JOIN superage."subscribers" s ON LOWER(TRIM(s.email)) = c.email
GROUP BY ROLLUP (c.utm_signal)
ORDER BY subs DESC;
