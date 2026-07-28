-- =============================================================================
-- Verify Slack report numbers: "Week of Jul 22–Jul 28, 2026"
-- Expected: 980 total | 1 Taboola | 614 Meta | 351 Others | 14 organic | 0 unknown
--
-- Window logic (from lambda):
--   end_day   = utcnow().date()      = 2026-07-28
--   start_day = end_day - 6 days     = 2026-07-22
--   SQL:  date_joined >= start_day AND date_joined < end_day + 1 day
--   Filter: state = 'active' (applied BEFORE dedup via DISTINCT ON email)
-- =============================================================================

-- 1. Exact replica of the lambda query — should match all 6 numbers.
WITH base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email))                          AS email,
        mv.source_label                               AS source_label,
        COALESCE(TRIM(mv.acquisition_utm_source), '') AS acq_utm,
        COALESCE(TRIM(mv.sub_source), '')             AS sub_src,
        COALESCE(TRIM(mv.source), '')                 AS src,
        COALESCE(TRIM(mv.utm_source), '')             AS utm_src,
        COALESCE(TRIM(mv.url_variables), '')          AS url_vars
    FROM superage."subscribers" s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-22'
      AND s.date_joined <  '2026-07-29'
      AND s.email IS NOT NULL
      AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state, ''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
),
classified AS (
    SELECT
        email,
        source_label,
        CASE
            WHEN source_label = 'Taboola' THEN 'taboola'
            WHEN source_label = 'Meta'    THEN 'meta'
            WHEN source_label = 'Organic' OR source_label IS NULL THEN
                CASE
                    WHEN acq_utm = '' AND sub_src = '' AND src = '' AND utm_src = '' AND url_vars = ''
                        THEN 'unknown'
                    ELSE 'organic'
                END
            ELSE 'other_brands'
        END AS source_bucket
    FROM base
)
SELECT
    COUNT(*)                                                AS new_subscribers,   -- expect 980
    COUNT(*) FILTER (WHERE source_bucket = 'taboola')      AS taboola,            -- expect 1
    COUNT(*) FILTER (WHERE source_bucket = 'meta')         AS meta,               -- expect 614
    COUNT(*) FILTER (WHERE source_bucket = 'other_brands') AS others,             -- expect 351
    COUNT(*) FILTER (WHERE source_bucket = 'organic')      AS organic,            -- expect 14
    COUNT(*) FILTER (WHERE source_bucket = 'unknown')      AS unknown             -- expect 0
FROM classified;


-- 2. Break down "others" by source_label to see what's inside.
WITH base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email))   AS email,
        mv.source_label        AS source_label
    FROM superage."subscribers" s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-22'
      AND s.date_joined <  '2026-07-29'
      AND s.email IS NOT NULL
      AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state, ''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
)
SELECT
    COALESCE(source_label, '(NULL/Organic)') AS source_label,
    COUNT(*) AS subs
FROM base
WHERE source_label NOT IN ('Taboola', 'Meta', 'Organic')
   OR source_label IS NULL
GROUP BY 1
ORDER BY 2 DESC;


-- 3. Sanity: total active subs joined this window, no dedup (should be >= 980 if dupes exist).
SELECT COUNT(*) AS raw_rows, COUNT(DISTINCT LOWER(TRIM(email))) AS distinct_emails
FROM superage."subscribers"
WHERE date_joined >= '2026-07-22'
  AND date_joined <  '2026-07-29'
  AND email IS NOT NULL
  AND TRIM(email) <> ''
  AND LOWER(TRIM(COALESCE(state, ''))) = 'active';


-- 4. How many of the 980 are in mv_subscriber_acquisition (MV populated)?
WITH base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email)) AS email,
        (mv.email IS NOT NULL) AS in_mv
    FROM superage."subscribers" s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-22'
      AND s.date_joined <  '2026-07-29'
      AND s.email IS NOT NULL
      AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state, ''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
)
SELECT
    COUNT(*)                              AS total,
    COUNT(*) FILTER (WHERE in_mv)        AS in_mv,
    COUNT(*) FILTER (WHERE NOT in_mv)    AS not_in_mv
FROM base;
