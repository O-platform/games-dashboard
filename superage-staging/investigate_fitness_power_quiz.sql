-- ============================================================
-- Investigation: fitness_power_quiz — 670 subs in sub_source
-- Run in pgAdmin / DBeaver / psql against the superage DB
-- ============================================================


-- 1. Full attribution picture: all fields side by side
SELECT
    COALESCE(NULLIF(TRIM(s.utm_source),''),  '(empty)') AS utm_source,
    COALESCE(NULLIF(TRIM(s.source),''),      '(empty)') AS source,
    COALESCE(NULLIF(TRIM(s.sub_source),''),  '(empty)') AS sub_source,
    COALESCE(NULLIF(TRIM(s.o_event),''),     '(empty)') AS o_event,
    COALESCE(NULLIF(TRIM(sa.acquisition_utm_source),''), '(not in acq table)') AS acq_utm_source,
    COUNT(*) AS cnt
FROM superage.subscribers s
LEFT JOIN superage.subscriber_acquisition sa
    ON LOWER(TRIM(s.email)) = LOWER(TRIM(sa.email))
WHERE LOWER(TRIM(s.sub_source)) = 'fitness_power_quiz'
GROUP BY 1,2,3,4,5
ORDER BY cnt DESC;


-- 2. Are they in the acquisition table at all?
SELECT
    CASE WHEN sa.email IS NOT NULL THEN 'In acq table' ELSE 'NOT in acq table' END AS acq_presence,
    COUNT(*) AS cnt
FROM superage.subscribers s
LEFT JOIN superage.subscriber_acquisition sa
    ON LOWER(TRIM(s.email)) = LOWER(TRIM(sa.email))
WHERE LOWER(TRIM(s.sub_source)) = 'fitness_power_quiz'
GROUP BY 1;


-- 3. Monthly trend — when did these 670 join?
SELECT
    TO_CHAR(DATE_TRUNC('month', date_joined), 'YYYY-MM') AS month,
    COUNT(*) AS cnt
FROM superage.subscribers
WHERE LOWER(TRIM(sub_source)) = 'fitness_power_quiz'
GROUP BY 1
ORDER BY 1 DESC;


-- 4. How does our attribution chain classify them?
--    Shows which level of the chain fires for each group
SELECT
    COALESCE(NULLIF(TRIM(sa.acquisition_utm_source),''), '(empty)')  AS L1_acq_utm,
    COALESCE(NULLIF(TRIM(s.sub_source),''), '(empty)')               AS L3_sub_source,
    COALESCE(NULLIF(TRIM(s.source),''), '(empty)')                   AS L4_source,
    COALESCE(NULLIF(TRIM(s.utm_source),''), '(empty)')               AS L5_utm_source,
    COALESCE(NULLIF(TRIM(s.o_event),''), '(empty)')                  AS o_event,
    COUNT(*) AS cnt,
    -- What the chain would assign them
    CASE
        WHEN LOWER(TRIM(sa.acquisition_utm_source)) IN ('facebook','meta','fb','ig') THEN 'Meta (L1)'
        WHEN sa.acquisition_utm_source IS NOT NULL AND TRIM(sa.acquisition_utm_source) <> ''
            AND LOWER(TRIM(sa.acquisition_utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a')
            THEN 'Other brand (L1): ' || TRIM(sa.acquisition_utm_source)
        WHEN LOWER(TRIM(s.sub_source)) IN ('facebook','meta','fb','ig')              THEN 'Meta (L3)'
        WHEN LOWER(TRIM(s.sub_source)) NOT IN ('none','null','(none)','(null)','-','n/a')
            AND TRIM(s.sub_source) <> ''                                             THEN 'Other brand (L3): ' || TRIM(s.sub_source)
        WHEN LOWER(TRIM(s.source)) IN ('facebook','meta','fb','ig')                  THEN 'Meta (L4)'
        WHEN LOWER(TRIM(s.source)) NOT IN ('none','null','(none)','(null)','-','n/a')
            AND TRIM(s.source) <> ''                                                 THEN 'Other brand (L4): ' || TRIM(s.source)
        WHEN LOWER(TRIM(s.utm_source)) IN ('facebook','meta','fb','ig')              THEN 'Meta (L5)'
        WHEN LOWER(TRIM(s.utm_source)) NOT IN ('none','null','(none)','(null)','-','n/a')
            AND TRIM(s.utm_source) <> ''                                             THEN 'Other brand (L5): ' || TRIM(s.utm_source)
        ELSE 'Organic'
    END AS chain_result
FROM superage.subscribers s
LEFT JOIN superage.subscriber_acquisition sa
    ON LOWER(TRIM(s.email)) = LOWER(TRIM(sa.email))
WHERE LOWER(TRIM(s.sub_source)) = 'fitness_power_quiz'
GROUP BY 1,2,3,4,5,6
ORDER BY cnt DESC;


-- 5. Sample of 30 rows — all fields visible
SELECT
    LEFT(s.email,5)||'***'                                            AS email_mask,
    s.date_joined::date                                               AS joined,
    COALESCE(NULLIF(TRIM(sa.acquisition_utm_source),''), '(empty)')  AS acq_utm,
    COALESCE(NULLIF(TRIM(s.utm_source),''),  '(empty)')              AS utm_source,
    COALESCE(NULLIF(TRIM(s.source),''),      '(empty)')              AS source,
    COALESCE(NULLIF(TRIM(s.sub_source),''),  '(empty)')              AS sub_source,
    COALESCE(NULLIF(TRIM(s.o_event),''),     '(empty)')              AS o_event,
    COALESCE(NULLIF(TRIM(s.state),''),       '(empty)')              AS state
FROM superage.subscribers s
LEFT JOIN superage.subscriber_acquisition sa
    ON LOWER(TRIM(s.email)) = LOWER(TRIM(sa.email))
WHERE LOWER(TRIM(s.sub_source)) = 'fitness_power_quiz'
ORDER BY s.date_joined DESC
LIMIT 30;
