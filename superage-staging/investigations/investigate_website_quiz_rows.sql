-- =============================================================================
-- Actual rows behind two Website groups (Week Jul 18–24, 2026, active)
--   A) the 5:  sub_source=fitness_power_quiz · source=website · utm=facebook/ig
--   B) source=website AND sub_source=longevity_quiz
-- Shows url_variables + o_event so we can see the real ad signal (if any).
-- Email is masked (first 5 chars).
-- =============================================================================

-- A) The 5 edge-case subs (fitness_power_quiz + website + facebook/ig)
SELECT
    LEFT(email, 5) || '***'                       AS email_mask,
    date_joined::date                             AS joined,
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COALESCE(NULLIF(TRIM(o_event),''),'(none)')                AS o_event,
    COALESCE(NULLIF(TRIM(url_variables),''),'(none)')          AS url_variables,
    source_label
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND LOWER(TRIM(sub_source)) = 'fitness_power_quiz'
  AND LOWER(TRIM(source))     = 'website'
  AND LOWER(TRIM(utm_source)) IN ('facebook','ig','fb','meta')
ORDER BY date_joined;


-- B) source=website AND sub_source=longevity_quiz  (sample up to 60)
SELECT
    LEFT(email, 5) || '***'                       AS email_mask,
    date_joined::date                             AS joined,
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COALESCE(NULLIF(TRIM(o_event),''),'(none)')                AS o_event,
    COALESCE(NULLIF(TRIM(url_variables),''),'(none)')          AS url_variables,
    source_label
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND LOWER(TRIM(source))     = 'website'
  AND LOWER(TRIM(sub_source)) = 'longevity_quiz'
ORDER BY date_joined
LIMIT 60;


-- B2) …and the url_variables pattern summary for group B (is there a hidden
--     utm_source=meta or a landing-page hint?)
SELECT
    COALESCE(NULLIF(TRIM(url_variables),''),'(none)') AS url_variables,
    COALESCE(NULLIF(TRIM(o_event),''),'(none)')       AS o_event,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND LOWER(TRIM(source))     = 'website'
  AND LOWER(TRIM(sub_source)) = 'longevity_quiz'
GROUP BY 1,2
ORDER BY subs DESC;
