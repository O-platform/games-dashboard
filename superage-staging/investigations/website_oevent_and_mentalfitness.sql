-- =============================================================================
-- (A) Website subs by o_event  +  (B) mentalfitness.network deep dive
-- Week Jul 18–24, 2026, active (unless noted all-time). Refresh MV first.
-- =============================================================================


-- A1. Website subs by o_event — how many longevity vs fitness quiz
SELECT
    COALESCE(NULLIF(TRIM(o_event),''),'(empty)') AS o_event,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
GROUP BY 1
ORDER BY subs DESC;


-- A2. Website subs: o_event × utm signal (word-of-mouth vs had an ad tag)
SELECT
    COALESCE(NULLIF(TRIM(o_event),''),'(empty)') AS o_event,
    CASE
        WHEN LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig') THEN 'utm=Meta'
        WHEN COALESCE(TRIM(utm_source),'')='' OR LOWER(TRIM(utm_source)) IN ('none','null','(none)','(null)','-','n/a') THEN 'utm=empty (word-of-mouth)'
        ELSE 'utm=other'
    END AS utm_signal,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
GROUP BY 1,2
ORDER BY subs DESC;


-- ============================================================================
-- B) mentalfitness.network deep dive
-- ============================================================================

-- B1. This week — raw field pattern (which level labels it, what o_event)
SELECT
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COALESCE(NULLIF(TRIM(o_event),''),'(none)')                AS o_event,
    COALESCE(NULLIF(TRIM(url_variables),''),'(none)')          AS url_variables,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'mentalfitness.network'
GROUP BY 1,2,3,4,5,6
ORDER BY subs DESC;


-- B2. All-time volume + monthly trend (is it new? growing?)
SELECT
    TO_CHAR(DATE_TRUNC('month', date_joined), 'YYYY-MM') AS month,
    COUNT(*)                                             AS subs,
    COUNT(*) FILTER (WHERE LOWER(TRIM(state))='active')  AS active_now
FROM superage.mv_subscriber_acquisition
WHERE source_label = 'mentalfitness.network'
GROUP BY 1
ORDER BY 1 DESC
LIMIT 24;


-- B3. All-time which chain field carries 'mentalfitness.network'
SELECT
    COUNT(*) FILTER (WHERE LOWER(TRIM(acquisition_utm_source)) = 'mentalfitness.network') AS in_acq_utm,
    COUNT(*) FILTER (WHERE LOWER(TRIM(sub_source))             = 'mentalfitness.network') AS in_sub_source,
    COUNT(*) FILTER (WHERE LOWER(TRIM(source))                 = 'mentalfitness.network') AS in_source,
    COUNT(*) FILTER (WHERE LOWER(TRIM(utm_source))             = 'mentalfitness.network') AS in_utm_source
FROM superage.mv_subscriber_acquisition
WHERE source_label = 'mentalfitness.network';


-- B4. All-time o_event mix for mentalfitness (what are they signing up for?)
SELECT
    COALESCE(NULLIF(TRIM(o_event),''),'(empty)') AS o_event,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE source_label = 'mentalfitness.network'
GROUP BY 1
ORDER BY subs DESC;
