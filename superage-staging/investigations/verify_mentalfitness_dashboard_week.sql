-- =============================================================================
-- Verify mentalfitness.network = 74 in the dashboard Weekly Digest (Jul 20–26).
-- Dashboard weekly-digest basis (comparison lambda): COALESCE(date_subscribed,
-- date_joined), ISO week Mon–Sun, NO state filter. Refresh MV first.
-- =============================================================================

-- 1. mentalfitness.network for Jul 20–26 under several bases (find which = 74)
SELECT 'date_joined, all states'        AS basis, COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE source_label = 'mentalfitness.network'
  AND date_joined >= '2026-07-20' AND date_joined < '2026-07-27'
UNION ALL
SELECT 'date_joined, active only', COUNT(*)
FROM superage.mv_subscriber_acquisition
WHERE source_label = 'mentalfitness.network'
  AND date_joined >= '2026-07-20' AND date_joined < '2026-07-27'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
UNION ALL
SELECT 'COALESCE(date_subscribed,date_joined), all states', COUNT(*)
FROM superage.mv_subscriber_acquisition
WHERE source_label = 'mentalfitness.network'
  AND COALESCE(date_subscribed, date_joined) >= '2026-07-20'
  AND COALESCE(date_subscribed, date_joined) < '2026-07-27'
UNION ALL
SELECT 'COALESCE(date_subscribed,date_joined), active only', COUNT(*)
FROM superage.mv_subscriber_acquisition
WHERE source_label = 'mentalfitness.network'
  AND COALESCE(date_subscribed, date_joined) >= '2026-07-20'
  AND COALESCE(date_subscribed, date_joined) < '2026-07-27'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active';


-- 2. Full source breakdown for the dashboard week (comparison-lambda basis).
--    Confirms fitness_power_quiz is GONE under the corrected chain (folds to Meta)
--    and shows mentalfitness.network's real position.
SELECT source_label, COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE COALESCE(date_subscribed, date_joined) >= '2026-07-20'
  AND COALESCE(date_subscribed, date_joined) < '2026-07-27'
GROUP BY 1
ORDER BY 2 DESC
LIMIT 20;
