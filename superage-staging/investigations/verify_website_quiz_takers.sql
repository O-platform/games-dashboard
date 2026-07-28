-- =============================================================================
-- Verify the "88 Website subs took the Longevity Quiz" claim.
-- Website (source_label) subs for Jul 18–24 with o_event=longivity_quiz,
-- cross-checked against subscribers.has_taken_longevity_quiz. Refresh MV first.
-- =============================================================================

-- 1. Summary — do the o_event=longevity Website subs actually show the quiz flag?
SELECT
    COUNT(*)                                                                   AS website_oevent_longevity,
    COUNT(*) FILTER (WHERE s.has_taken_longevity_quiz = true)                   AS flag_confirms_took_quiz,
    COUNT(*) FILTER (WHERE s.has_taken_longevity_quiz IS NOT TRUE)              AS flag_says_not_taken
FROM superage.mv_subscriber_acquisition mv
JOIN superage."subscribers" s ON LOWER(TRIM(s.email)) = mv.email
WHERE mv.date_joined >= '2026-07-18' AND mv.date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(mv.state,''))) = 'active'
  AND mv.source_label = 'Website'
  AND LOWER(TRIM(mv.o_event)) = 'longivity_quiz';


-- 2. The list — one row per sub (masked email) so you can eyeball / edit the msg
SELECT
    LEFT(mv.email, 5) || '***'                      AS email_mask,
    mv.date_joined::date                            AS joined,
    COALESCE(NULLIF(TRIM(mv.sub_source),''),'(none)') AS sub_source,
    COALESCE(NULLIF(TRIM(mv.source),''),'(none)')     AS source,
    COALESCE(NULLIF(TRIM(mv.utm_source),''),'(none)') AS utm_source,
    mv.o_event,
    s.has_taken_longevity_quiz                      AS took_longevity,
    s.took_fitness_quiz                             AS took_fitness_raw
FROM superage.mv_subscriber_acquisition mv
JOIN superage."subscribers" s ON LOWER(TRIM(s.email)) = mv.email
WHERE mv.date_joined >= '2026-07-18' AND mv.date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(mv.state,''))) = 'active'
  AND mv.source_label = 'Website'
  AND LOWER(TRIM(mv.o_event)) = 'longivity_quiz'
ORDER BY mv.date_joined;
