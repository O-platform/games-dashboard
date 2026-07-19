-- ============================================================
-- Investigation: fitness_power_quiz attribution pattern
-- Run in pgAdmin / DBeaver / psql against the superage DB
-- ============================================================


-- 1. Which field drives 'fitness_power_quiz'?
SELECT
    COUNT(*) FILTER (WHERE LOWER(TRIM(sub_source)) = 'fitness_power_quiz') AS in_sub_source,
    COUNT(*) FILTER (WHERE LOWER(TRIM(source))     = 'fitness_power_quiz') AS in_source,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source)) = 'fitness_power_quiz') AS in_utm_source
FROM superage.subscribers;


-- 2. Cross-tab: utm_source x sub_source x o_event for these subs
SELECT
    COALESCE(NULLIF(TRIM(utm_source),''), '(empty)') AS utm_source,
    COALESCE(NULLIF(TRIM(sub_source),''), '(empty)') AS sub_source,
    COALESCE(NULLIF(TRIM(o_event),''),   '(empty)') AS o_event,
    COUNT(*) AS cnt
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
GROUP BY 1,2,3
ORDER BY cnt DESC;


-- 3. Monthly trend — last 18 months, split by utm_source value
SELECT
    TO_CHAR(DATE_TRUNC('month', date_joined), 'YYYY-MM') AS month,
    COUNT(*)                                                                         AS total,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')) AS utm_is_meta,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source)) = 'fitness_power_quiz')           AS utm_is_fpq,
    COUNT(*) FILTER (WHERE TRIM(utm_source) = '' OR utm_source IS NULL)              AS utm_empty_or_null
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
  AND date_joined >= NOW() - INTERVAL '18 months'
GROUP BY 1
ORDER BY 1 DESC;


-- 4. o_event distribution across all fitness_power_quiz subs (all time)
SELECT
    COALESCE(NULLIF(TRIM(o_event),''), '(empty)') AS o_event,
    COUNT(*)              AS cnt,
    MIN(date_joined::date) AS first_seen,
    MAX(date_joined::date) AS last_seen
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
GROUP BY 1
ORDER BY cnt DESC;


-- 5. What does acquisition_utm_source say for these subs?
SELECT
    COALESCE(NULLIF(TRIM(sa.acquisition_utm_source),''), '(empty/null)') AS acq_utm,
    COUNT(*) AS cnt
FROM superage.subscribers s
JOIN superage.subscriber_acquisition sa
    ON LOWER(TRIM(s.email)) = LOWER(TRIM(sa.email))
WHERE LOWER(TRIM(s.source)) = 'fitness_power_quiz'
GROUP BY 1
ORDER BY cnt DESC
LIMIT 20;


-- 6. Sample of 20 recent subs (last 60 days)
SELECT
    LEFT(email,4)||'***'                               AS email_mask,
    date_joined::date                                  AS joined,
    COALESCE(NULLIF(TRIM(utm_source),''), '(empty)')  AS utm_source,
    COALESCE(NULLIF(TRIM(sub_source),''), '(empty)')  AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),    '(empty)')   AS source,
    COALESCE(NULLIF(TRIM(o_event),''),   '(empty)')   AS o_event
FROM superage.subscribers
WHERE LOWER(TRIM(source)) = 'fitness_power_quiz'
  AND date_joined >= NOW() - INTERVAL '60 days'
ORDER BY date_joined DESC
LIMIT 20;
