-- =============================================================================
-- "Other brands" cohort from the Jul 18–24 report — status since the report
-- Report sent: Fri 2026-07-24.  Today: 2026-07-28.
-- Question: of the other-brands new subs that week, how many have unsubscribed
--           between the report date and now (i.e. how "sticky" are they)?
-- =============================================================================
-- Cohort = subs whose date_joined is in the report week AND whose CORRECTED
-- source is a non-Meta/Taboola/Organic brand. We do NOT filter on current
-- state here (that's the whole point — we want to see who has since churned).
-- Current state + unsubscribe date come from the live subscribers table.

WITH cohort AS (
    SELECT mv.email, mv.source_label
    FROM superage.mv_subscriber_acquisition mv
    WHERE mv.date_joined >= '2026-07-18' AND mv.date_joined < '2026-07-25'
      AND mv.source_label NOT IN ('Taboola','Meta','Organic')
      AND mv.source_label IS NOT NULL
)
SELECT
    COUNT(*)                                                                   AS other_brands_that_week,
    COUNT(*) FILTER (WHERE LOWER(TRIM(s.state)) = 'active')                     AS still_active,
    COUNT(*) FILTER (WHERE LOWER(TRIM(s.state)) = 'unsubscribed')               AS unsubscribed_now,
    COUNT(*) FILTER (WHERE LOWER(TRIM(s.state)) = 'bounced')                    AS bounced_now,
    COUNT(*) FILTER (WHERE s.date_unsubscribed::date >= '2026-07-24')           AS unsubbed_since_report,
    ROUND(100.0 * COUNT(*) FILTER (WHERE s.date_unsubscribed::date >= '2026-07-24')
          / NULLIF(COUNT(*), 0), 1)                                            AS pct_unsubbed_since_report
FROM cohort c
JOIN superage.subscribers s ON LOWER(TRIM(s.email)) = c.email;


-- Same cut, broken down by brand — which "other brand" churns fastest
WITH cohort AS (
    SELECT mv.email, mv.source_label
    FROM superage.mv_subscriber_acquisition mv
    WHERE mv.date_joined >= '2026-07-18' AND mv.date_joined < '2026-07-25'
      AND mv.source_label NOT IN ('Taboola','Meta','Organic')
      AND mv.source_label IS NOT NULL
)
SELECT
    c.source_label,
    COUNT(*)                                                          AS subs,
    COUNT(*) FILTER (WHERE LOWER(TRIM(s.state)) = 'active')           AS still_active,
    COUNT(*) FILTER (WHERE s.date_unsubscribed::date >= '2026-07-24') AS unsubbed_since_report
FROM cohort c
JOIN superage.subscribers s ON LOWER(TRIM(s.email)) = c.email
GROUP BY c.source_label
ORDER BY subs DESC;
