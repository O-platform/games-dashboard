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
--
--    NOTE: Postgres does not support DISTINCT inside a window-function
--    aggregate (COUNT(DISTINCT x) OVER (...) errors with "DISTINCT is not
--    implemented for window functions"), even though the identical
--    COUNT(DISTINCT x) works fine as a plain GROUP BY aggregate (queries
--    1 and 2 above are unaffected). This DELETE only needs to know
--    whether every value in the partition is IDENTICAL, so
--    MIN(x) OVER (...) = MAX(x) OVER (...) is used instead — it answers
--    exactly that question without needing DISTINCT in a window function.
WITH grouped AS (
    SELECT
        id,
        issue_name,
        RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/') AS url_key,
        unique_clicks,
        non_unique_clicks,
        COUNT(*) OVER w AS fragment_rows,
        (MIN(unique_clicks) OVER w = MAX(unique_clicks) OVER w)
            AS unique_values_consistent,
        (MIN(non_unique_clicks) OVER w = MAX(non_unique_clicks) OVER w)
            AS non_unique_values_consistent,
        ROW_NUMBER() OVER (
            PARTITION BY issue_name, RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/')
            ORDER BY id ASC
        ) AS rn
    FROM superage.articles_clicks
    WINDOW w AS (PARTITION BY issue_name, RTRIM(SPLIT_PART(TRIM(url), '?', 1), '/'))
)
DELETE FROM superage.articles_clicks ac
USING grouped g
WHERE ac.id = g.id
  AND g.fragment_rows > 1
  AND g.unique_values_consistent
  AND g.non_unique_values_consistent
  AND g.rn > 1;
