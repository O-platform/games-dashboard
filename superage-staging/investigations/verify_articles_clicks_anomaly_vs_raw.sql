-- =============================================================================
-- Is the "non_unique_clicks < unique_clicks" anomaly in superage.articles_clicks
-- a bug in the ETL that BUILDS articles_clicks, or does it already exist in the
-- raw Campaign Monitor click log (superage."Campaigns_Clicks")?
--
-- Method: independently recompute unique_clicks (COUNT DISTINCT email) and
-- non_unique_clicks (COUNT *) directly from Campaigns_Clicks for the exact
-- same (url, issue_name) pairs that showed the anomaly in articles_clicks,
-- then compare:
--   - raw counts sane (non_unique >= unique)  -> bug is in the articles_clicks
--     ETL/aggregation step, not the source data.
--   - raw counts ALSO show non_unique < unique -> the anomaly already exists
--     upstream (CM export itself, or the Campaigns_Clicks ingestion).
-- =============================================================================

WITH offenders (url, issue_name, ac_unique_clicks, ac_non_unique_clicks) AS (
    VALUES
    ('https://superage.com/7-longevity-habits-that-cost-nothing',                                              'Sunday Spotlight: August 2, 2026 (AG1)',                                          17942, 2399),
    ('https://superage.com/what-actually-increases-your-energy-the-buck-institute-just-figured-it-out',        'Sunday Spotlight: June 14, 2026 (Babbel)',                                        16729, 4614),
    ('https://superage.com/120-minutes-of-weekly-strength-work-is-best-for-longevity-heres-what-to-do',        'Sunday Spotlight: August 9, 2026 (Wisp)',                                         11666, 1583),
    ('https://superage.com/3-ways-to-protect-your-working-memory-as-you-age',                                  'Sunday Spotlight: July 26, 2026 (Aramore)',                                       10775, 2930),
    ('https://standard.superage.com/try/ifZwv0qfHo8wt8uhAhqXm3dg',                                             'Standard Email 1 - GROUP 2: BEGINNER - Tuesday, September 1, 2026',               9096, 4336),
    ('https://superage.com/this-o-g-longevity-doc-wants-you-to-experiment-on-yourself',                        'Sunday Spotlight: August 23, 2026 (No ad)',                                        4990,  590),
    ('https://superage.com/your-exposome-is-shaping-how-fast-you-age-heres-what-to-do',                        'Sunday Spotlight: August 16, 2026 (HSA)',                                          4773,  668),
    ('https://superage.com/awecourse',                                                                          'The Power of Awe Launch (July 18, 2026)',                                          4878, 1096),
    ('https://superage.com/meditation-may-slow-the-aging-process-and-more',                                    'Sunday Spotlight: September 6, 2026 (Fatty15)',                                    4400,  653),
    ('https://superage.com/the-protein-threshold-where-longevity-benefits-stop-climbing',                       'Sunday Spotlight: July 19, 2026 (Braun)',                                          7294, 3570),
    ('https://superage.com/this-one-change-to-your-exercise-routine-could-add-years-to-your-life',              'Sunday Spotlight: June 21, 2026 (Wisp)',                                           4457, 1198),
    ('https://superage.com/design-your-own-unbreakable-longevity-plan',                                        'Sunday Spotlight: July 5, 2026 (Butcher Box)',                                     4282, 1147),
    ('https://superage.com/she-trained-for-strength-now-shes-training-what-scares-her',                        'Sunday Spotlight: June 28, 2026 (Pendulum)',                                       3826,  818),
    ('https://superage.com/standard-gamesbeta',                                                                 'First Dedicated Send for The Standard: David''s Letter - Thursday, August 20, 2026', 4825, 2248),
    ('https://superage.com/why-you-should-add-jumping-to-your-workout-routine',                                'The Mindset: August 10, 2026 (NeuroTracker)',                                     19947, 17371)
),
norm AS (
    SELECT
        o.*,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(o.url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
    FROM offenders o
),
raw AS (
    SELECT
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(cc."URL", '?', 1))), '^https?://(www[.])?', '') AS norm_url,
        TRIM(cc."issue_name") AS issue_name,
        COUNT(DISTINCT LOWER(TRIM(cc."EmailAddress"))) AS raw_unique_clicks,
        COUNT(*)                                       AS raw_non_unique_clicks
        -- swap cc."EmailAddress" for cc."EmailAddress " (trailing space) if this errors
    FROM superage."Campaigns_Clicks" cc
    GROUP BY 1, 2
)
SELECT
    n.issue_name,
    n.url,
    n.ac_unique_clicks,
    n.ac_non_unique_clicks,
    (n.ac_unique_clicks - n.ac_non_unique_clicks) AS ac_gap,
    r.raw_unique_clicks,
    r.raw_non_unique_clicks,
    (r.raw_unique_clicks - r.raw_non_unique_clicks) AS raw_gap,
    CASE
        WHEN r.norm_url IS NULL THEN 'NO MATCH in Campaigns_Clicks (issue_name/url mismatch)'
        WHEN r.raw_unique_clicks > r.raw_non_unique_clicks THEN 'ANOMALY ALSO IN RAW — upstream/CM issue'
        ELSE 'raw looks sane — bug is in articles_clicks ETL'
    END AS diagnosis
FROM norm n
LEFT JOIN raw r
       ON r.norm_url = n.norm_url
      AND LOWER(r.issue_name) = LOWER(TRIM(n.issue_name))
ORDER BY n.ac_unique_clicks - n.ac_non_unique_clicks DESC;
