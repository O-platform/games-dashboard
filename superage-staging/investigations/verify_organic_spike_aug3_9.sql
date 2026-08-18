-- =============================================================================
-- Investigate the "New Organic Subscribers — Weekly" chart spike:
--   flat ~0 for months, then 3–9 Aug 2026 = 1,430 organic subs.
--
-- Chart basis (superage_comparison_lambda.py, top_source_rows query):
--   date field : COALESCE(date_subscribed, date_joined)
--   bucket     : COALESCE(mv.source_label, 'Organic')   -- mv = mv_subscriber_acquisition
--   NO state filter (counts subs regardless of active/unsubscribed).
--   Excludes the current in-progress week.
-- Refresh the MV first: REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_subscriber_acquisition;
-- =============================================================================

-- 1. Reproduce the chart exactly for the last 6 completed weeks — confirms 1,430.
WITH src AS (
    SELECT
        DATE_TRUNC('week', COALESCE(s.date_subscribed, s.date_joined)::date)::date AS week_start,
        COALESCE(mv.source_label, 'Organic') AS bucket
    FROM superage."subscribers" s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE COALESCE(s.date_subscribed, s.date_joined) IS NOT NULL
      AND COALESCE(s.date_subscribed, s.date_joined)::date >= DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '6 weeks'
      AND COALESCE(s.date_subscribed, s.date_joined)::date <  DATE_TRUNC('week', CURRENT_DATE)::date
)
SELECT week_start, bucket, COUNT(*) AS subs
FROM src
WHERE bucket = 'Organic'
GROUP BY 1, 2
ORDER BY 1 DESC;


-- 2. Same week (3–9 Aug), ALL buckets — is this an organic-only spike, or did
--    total new subs spike across the board (e.g. a viral moment / press hit)?
WITH src AS (
    SELECT
        DATE_TRUNC('week', COALESCE(s.date_subscribed, s.date_joined)::date)::date AS week_start,
        COALESCE(mv.source_label, 'Organic') AS bucket
    FROM superage."subscribers" s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE COALESCE(s.date_subscribed, s.date_joined) IS NOT NULL
      AND COALESCE(s.date_subscribed, s.date_joined)::date >= '2026-08-03'
      AND COALESCE(s.date_subscribed, s.date_joined)::date <  '2026-08-10'
)
SELECT bucket, COUNT(*) AS subs
FROM src
GROUP BY 1
ORDER BY 2 DESC;


-- 3. Are the "Organic" subs in this window even IN the MV? If the MV is stale
--    (hasn't refreshed since these subs joined), they'd fall through to the
--    COALESCE(...,'Organic') default even though they may have real signals
--    once the MV catches up. This would be a false spike, not real organic growth.
SELECT
    (mv.email IS NOT NULL)                                   AS in_mv,
    COUNT(*)                                                 AS subs
FROM superage."subscribers" s
LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
WHERE COALESCE(s.date_subscribed, s.date_joined)::date >= '2026-08-03'
  AND COALESCE(s.date_subscribed, s.date_joined)::date <  '2026-08-10'
  AND COALESCE(mv.source_label, 'Organic') = 'Organic'
GROUP BY 1;


-- 4. For the ones that ARE in the MV and still resolved to Organic, what do
--    their raw chain fields actually look like? Are they truly blank
--    (no acq_utm / sub_source / source / utm_source / url_variables — real
--    organic), or is there a value we're failing to canonicalize?
SELECT
    COALESCE(NULLIF(TRIM(mv.acquisition_utm_source), ''), '(blank)') AS acq_utm,
    COALESCE(NULLIF(TRIM(mv.sub_source), ''), '(blank)')             AS sub_source,
    COALESCE(NULLIF(TRIM(mv.source), ''), '(blank)')                 AS source,
    COALESCE(NULLIF(TRIM(mv.utm_source), ''), '(blank)')             AS utm_source,
    COALESCE(NULLIF(TRIM(mv.url_variables), ''), '(blank)')          AS url_variables,
    COUNT(*)                                                         AS subs
FROM superage."subscribers" s
JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
WHERE COALESCE(s.date_subscribed, s.date_joined)::date >= '2026-08-03'
  AND COALESCE(s.date_subscribed, s.date_joined)::date <  '2026-08-10'
  AND mv.source_label = 'Organic'
GROUP BY 1, 2, 3, 4, 5
ORDER BY 6 DESC
LIMIT 30;


-- 5. Daily breakdown within the week — which day(s) drove the spike?
--    A spike concentrated on 1-2 days points to a single event (press hit,
--    App Store feature, social virality, or a bulk import); an even spread
--    across all 7 days looks more like organic-channel growth.
SELECT
    COALESCE(s.date_subscribed, s.date_joined)::date AS day,
    COUNT(*)                                          AS subs,
    COUNT(*) FILTER (WHERE mv.email IS NULL)          AS not_in_mv
FROM superage."subscribers" s
LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
WHERE COALESCE(s.date_subscribed, s.date_joined)::date >= '2026-08-03'
  AND COALESCE(s.date_subscribed, s.date_joined)::date <  '2026-08-10'
  AND COALESCE(mv.source_label, 'Organic') = 'Organic'
GROUP BY 1
ORDER BY 1;


-- 6. State breakdown — chart has NO state filter. If a large share of these
--    "organic" subs are already unsubscribed/bounced, the spike may reflect
--    something like a bulk list import/migration rather than fresh sign-ups.
SELECT
    LOWER(TRIM(COALESCE(s.state, '(blank)')))  AS state,
    COUNT(*)                                    AS subs
FROM superage."subscribers" s
LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
WHERE COALESCE(s.date_subscribed, s.date_joined)::date >= '2026-08-03'
  AND COALESCE(s.date_subscribed, s.date_joined)::date <  '2026-08-10'
  AND COALESCE(mv.source_label, 'Organic') = 'Organic'
GROUP BY 1
ORDER BY 2 DESC;


-- 7. Sample rows — eyeball a handful directly (masked email) to spot patterns
--    (same domain, same o_event, sequential IDs suggesting a bulk import, etc.)
SELECT
    LEFT(LOWER(TRIM(s.email)), 5) || '***'            AS email_mask,
    COALESCE(s.date_subscribed, s.date_joined)         AS joined_at,
    s.state,
    s.o_event,
    COALESCE(NULLIF(TRIM(s.acquisition_utm_source),''), '(blank)') AS acq_utm,
    COALESCE(NULLIF(TRIM(s.sub_source),''), '(blank)')              AS sub_source,
    COALESCE(NULLIF(TRIM(s.source),''), '(blank)')                  AS source,
    COALESCE(NULLIF(TRIM(s.utm_source),''), '(blank)')              AS utm_source
FROM superage."subscribers" s
LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
WHERE COALESCE(s.date_subscribed, s.date_joined)::date >= '2026-08-03'
  AND COALESCE(s.date_subscribed, s.date_joined)::date <  '2026-08-10'
  AND COALESCE(mv.source_label, 'Organic') = 'Organic'
ORDER BY joined_at
LIMIT 40;
