-- =============================================================================
-- Do quiz-named subs actually carry a real Meta signal?
-- =============================================================================
-- Question: for subscribers whose sub_source / source is a QUIZ name
--   (fitness_power_quiz, fitness_quiz, longivity_quiz, longevity_quiz),
--   how many ALSO have a genuine Meta signal somewhere in the chain
--   (acquisition_utm_source / utm_source / source = facebook|meta|fb|ig, or
--    url_variables utm_source=meta) vs. how many have NO Meta signal at all?
--
-- If ~all have a real Meta signal → we can DROP the quiz names from the Meta
--   mapping and the chain still lands on Meta via the real field (correct).
-- If a chunk have NO Meta signal → those are the ones my hardcoded mapping
--   was WRONGLY forcing into Meta; they should resolve to their real source.
--
-- Uses raw columns from the MV (independent of source_label logic).
-- =============================================================================

-- 1. Headline split
WITH q AS (
    SELECT
        email, sub_source, source, utm_source, acquisition_utm_source, url_variables,
        (   LOWER(TRIM(acquisition_utm_source)) IN ('facebook','meta','fb','ig')
         OR LOWER(TRIM(utm_source))             IN ('facebook','meta','fb','ig')
         OR LOWER(TRIM(source))                 IN ('facebook','meta','fb','ig')
         OR LOWER(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)'))) = 'meta'
        ) AS has_real_meta_signal
    FROM superage.mv_subscriber_acquisition
    WHERE LOWER(TRIM(sub_source)) IN ('fitness_power_quiz','fitness_quiz','longivity_quiz','longevity_quiz')
       OR LOWER(TRIM(source))     IN ('fitness_power_quiz','fitness_quiz','longivity_quiz','longevity_quiz')
)
SELECT
    COUNT(*)                                        AS total_quiz_named,
    COUNT(*) FILTER (WHERE has_real_meta_signal)     AS with_real_meta_signal,
    COUNT(*) FILTER (WHERE NOT has_real_meta_signal) AS WITHOUT_meta_signal,  -- <- these my mapping wrongly forced to Meta
    ROUND(100.0 * COUNT(*) FILTER (WHERE NOT has_real_meta_signal) / NULLIF(COUNT(*),0), 1) AS pct_without
FROM q;


-- 2. The subs WITHOUT any Meta signal — what would they REALLY resolve to?
--    (shows the quiz name is masking a Website / Campaign_monitor / blank source)
WITH q AS (
    SELECT sub_source, source, utm_source, acquisition_utm_source, url_variables
    FROM superage.mv_subscriber_acquisition
    WHERE (LOWER(TRIM(sub_source)) IN ('fitness_power_quiz','fitness_quiz','longivity_quiz','longevity_quiz')
        OR LOWER(TRIM(source))     IN ('fitness_power_quiz','fitness_quiz','longivity_quiz','longevity_quiz'))
      AND NOT (
            LOWER(TRIM(acquisition_utm_source)) IN ('facebook','meta','fb','ig')
         OR LOWER(TRIM(utm_source))             IN ('facebook','meta','fb','ig')
         OR LOWER(TRIM(source))                 IN ('facebook','meta','fb','ig')
         OR LOWER(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)'))) = 'meta'
      )
)
SELECT
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COUNT(*) AS subs
FROM q
GROUP BY 1,2,3,4
ORDER BY subs DESC
LIMIT 40;
