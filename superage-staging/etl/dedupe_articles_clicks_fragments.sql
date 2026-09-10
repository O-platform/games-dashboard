-- =============================================================================
-- One-time cleanup: collapse articles_clicks rows that are the SAME logical
-- article/campaign but got fragmented into multiple rows by the
-- sa_classify.py bugs (sponsor clean_url retaining per-subscriber query
-- params; strip_query() not lowercasing the path).
--
-- Safe to run AFTER lambda_clicks_incremental.py's `mode: all` rebuild has
-- completed — at that point every fragment sharing the same (issue_name,
-- query-stripped url) already shows the SAME correct unique_clicks /
-- non_unique_clicks (the rebuild's recompute matches on that stripped key,
-- so it wrote the identical true total onto every fragment). This script
-- just removes the now-redundant duplicates so a plain SUM(non_unique_clicks)
-- GROUP BY article in any downstream query doesn't over-count by summing
-- the same total N times across N fragments.
--
-- Run the SELECT first to see what would be removed. Only run the DELETE
-- once you're satisfied.
-- =============================================================================

-- 1. Preview: how many rows would collapse, and by how much, per group.
WITH grouped AS (
    SELECT
        issue_name,
        RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/')            AS url_key,
        COUNT(*)                                              AS fragment_rows,
        array_agg(DISTINCT article_title)                     AS titles_seen,
        array_agg(DISTINCT unique_clicks)                      AS unique_clicks_seen,
        array_agg(DISTINCT non_unique_clicks)                  AS non_unique_clicks_seen
    FROM superage.articles_clicks
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
)
SELECT *
FROM grouped
ORDER BY fragment_rows DESC
LIMIT 50;


-- 2. Sanity check BEFORE deleting: confirm every fragment in each group
--    really does show the same numbers (i.e. the rebuild already ran and
--    corrected them). If unique_clicks_seen / non_unique_clicks_seen show
--    more than one distinct value for a group, STOP — that group hasn't
--    been through the rebuild yet, or something else is off; don't dedupe
--    it until it has exactly one distinct value each.
WITH grouped AS (
    SELECT
        issue_name,
        RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/')            AS url_key,
        COUNT(*)                                              AS fragment_rows,
        COUNT(DISTINCT unique_clicks)                          AS distinct_unique_values,
        COUNT(DISTINCT non_unique_clicks)                      AS distinct_non_unique_values
    FROM superage.articles_clicks
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
)
SELECT
    COUNT(*)                                                          AS groups_with_fragments,
    COUNT(*) FILTER (WHERE distinct_unique_values = 1
                       AND distinct_non_unique_values = 1)            AS groups_safe_to_dedupe,
    COUNT(*) FILTER (WHERE distinct_unique_values > 1
                        OR distinct_non_unique_values > 1)            AS groups_NOT_yet_consistent
FROM grouped;


-- 3. The actual cleanup — keeps ONE row per (issue_name, url_key) group
--    (the lowest id, arbitrary but stable), deletes the rest. Only touches
--    groups where every fragment already agrees on unique_clicks AND
--    non_unique_clicks (the "groups_safe_to_dedupe" from query 2 above).
WITH grouped AS (
    SELECT
        id,
        issue_name,
        RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/') AS url_key,
        unique_clicks,
        non_unique_clicks,
        COUNT(*) OVER (PARTITION BY issue_name, RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/'))
            AS fragment_rows,
        COUNT(DISTINCT unique_clicks) OVER (PARTITION BY issue_name, RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/'))
            AS distinct_unique_values,
        COUNT(DISTINCT non_unique_clicks) OVER (PARTITION BY issue_name, RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/'))
            AS distinct_non_unique_values,
        ROW_NUMBER() OVER (
            PARTITION BY issue_name, RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/')
            ORDER BY id ASC
        ) AS rn
    FROM superage.articles_clicks
)
DELETE FROM superage.articles_clicks ac
USING grouped g
WHERE ac.id = g.id
  AND g.fragment_rows > 1
  AND g.distinct_unique_values = 1
  AND g.distinct_non_unique_values = 1
  AND g.rn > 1;
