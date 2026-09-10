-- =============================================================================
-- One-time backfill: for November 2025 articles_clicks rows only, recompute
-- unique_clicks / non_unique_clicks from optimism.superage_clicks +
-- optimism.superage_campaigns instead of superage."Campaigns_Clicks".
--
-- Why: November 2025 was the historical ETL ramp-up period for
-- superage."Campaigns_Clicks" — comparing against the optimism-schema raw
-- click log showed it's missing significant click volume for that window
-- specifically (gaps up to ~17K clicks on individual placements), while
-- December 2025 onward the two sources agree within <1%. This backfill is
-- SCOPED to November 2025 only — every row outside that window is
-- untouched, left exactly as the mode:all rebuild already computed it from
-- Campaigns_Clicks.
--
-- Matching: articles_clicks.issue_name <-> optimism.superage_campaigns.name
-- (case/whitespace-insensitive exact match), then articles_clicks.url <->
-- optimism.superage_clicks.url on the query-stripped base URL — the same
-- matching convention used everywhere else in this ETL.
--
-- Run the preview queries first. Only run the UPDATE once you're satisfied.
-- =============================================================================

-- 1. Preview: how many November 2025 rows would change, and by how much.
WITH matched AS (
    SELECT
        ac.id,
        ac.issue_name,
        ac.url,
        ac.unique_clicks         AS old_unique_clicks,
        ac.non_unique_clicks     AS old_non_unique_clicks,
        COUNT(DISTINCT oc.email) AS new_unique_clicks,
        COUNT(*)                 AS new_non_unique_clicks
    FROM superage.articles_clicks ac
    JOIN optimism.superage_campaigns ocamp
         ON LOWER(TRIM(ocamp.name)) = LOWER(TRIM(ac.issue_name))
    JOIN optimism.superage_clicks oc
         ON oc.campaign_id = ocamp.campaign_id
        AND RTRIM(SPLIT_PART(TRIM(oc.url), '?', 1), '/') = RTRIM(SPLIT_PART(TRIM(ac.url), '?', 1), '/')
    WHERE ac.issue_date >= '2025-11-01'
      AND ac.issue_date <  '2025-12-01'
    GROUP BY ac.id, ac.issue_name, ac.url, ac.unique_clicks, ac.non_unique_clicks
)
SELECT
    issue_name, url,
    old_unique_clicks, new_unique_clicks, (new_unique_clicks - old_unique_clicks) AS unique_delta,
    old_non_unique_clicks, new_non_unique_clicks, (new_non_unique_clicks - old_non_unique_clicks) AS non_unique_delta
FROM matched
ORDER BY (new_unique_clicks - old_unique_clicks) DESC
LIMIT 50;


-- 2. Coverage check: how many November 2025 rows in articles_clicks actually
--    found a match in the optimism schema? Rows with NO match won't be
--    touched by the UPDATE below — worth knowing how many that is before
--    you run it.
SELECT
    COUNT(*) AS nov_2025_rows_total,
    COUNT(*) FILTER (WHERE matched.id IS NOT NULL) AS nov_2025_rows_will_update,
    COUNT(*) FILTER (WHERE matched.id IS NULL)     AS nov_2025_rows_no_optimism_match
FROM superage.articles_clicks ac
LEFT JOIN (
    SELECT DISTINCT ac2.id
    FROM superage.articles_clicks ac2
    JOIN optimism.superage_campaigns ocamp
         ON LOWER(TRIM(ocamp.name)) = LOWER(TRIM(ac2.issue_name))
    JOIN optimism.superage_clicks oc
         ON oc.campaign_id = ocamp.campaign_id
        AND RTRIM(SPLIT_PART(TRIM(oc.url), '?', 1), '/') = RTRIM(SPLIT_PART(TRIM(ac2.url), '?', 1), '/')
    WHERE ac2.issue_date >= '2025-11-01'
      AND ac2.issue_date <  '2025-12-01'
) matched ON matched.id = ac.id
WHERE ac.issue_date >= '2025-11-01'
  AND ac.issue_date <  '2025-12-01';


-- 3. The actual backfill — ONLY touches rows with issue_date in November
--    2025. Everything else in articles_clicks is untouched.
WITH matched AS (
    SELECT
        ac.id,
        COUNT(DISTINCT oc.email) AS unique_clicks,
        COUNT(*)                 AS non_unique_clicks
    FROM superage.articles_clicks ac
    JOIN optimism.superage_campaigns ocamp
         ON LOWER(TRIM(ocamp.name)) = LOWER(TRIM(ac.issue_name))
    JOIN optimism.superage_clicks oc
         ON oc.campaign_id = ocamp.campaign_id
        AND RTRIM(SPLIT_PART(TRIM(oc.url), '?', 1), '/') = RTRIM(SPLIT_PART(TRIM(ac.url), '?', 1), '/')
    WHERE ac.issue_date >= '2025-11-01'
      AND ac.issue_date <  '2025-12-01'
    GROUP BY ac.id
)
UPDATE superage.articles_clicks ac
SET
    unique_clicks     = m.unique_clicks,
    non_unique_clicks = m.non_unique_clicks,
    updated_at        = NOW()
FROM matched m
WHERE ac.id = m.id;
