-- =============================================================================
-- Investigation: why the deployed Slack report under-counted Meta
-- Week of Jul 18–24, 2026  (date_joined in [2026-07-18, 2026-07-25), active)
-- =============================================================================
-- Hypothesis: the deployed Slack lambda lacks the fix (Meta campaign names like
-- fitness_power_quiz not mapped to Meta + missing DISTINCT ON), so it bucketed
-- ~500 real Meta subs as "other brands". The MV has the fix. These queries
-- show exactly which field/value earns each Meta sub its label — the
-- campaign-name block is the bug's contribution.
--
-- Queried directly against the MV (already one row per email).
-- =============================================================================


-- 1. Bucket counts for the week, straight from the MV (should tie to 874 / 212)
SELECT
    CASE
        WHEN source_label = 'Taboola' THEN 'taboola'
        WHEN source_label = 'Meta'    THEN 'meta'
        WHEN source_label = 'Organic' OR source_label IS NULL THEN 'organic/unknown'
        ELSE 'other_brands'
    END AS bucket,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
GROUP BY 1 ORDER BY 2 DESC;


-- 2. THE PROOF — for the week's Meta subs, what field + raw value produced 'Meta'?
--    A big 'fitness_power_quiz' / 'longivity_quiz' block = subs the OLD code
--    (which didn't map those names) would have thrown into other_brands/organic.
SELECT
    CASE
        WHEN superage.canon_source(acquisition_utm_source) = 'Meta'
            THEN 'L1 acquisition_utm_source: ' || LOWER(TRIM(acquisition_utm_source))
        WHEN LOWER(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)'))) = 'meta'
             AND COALESCE(date_subscribed, date_joined)::date >= '2025-11-01'
            THEN 'L2 url_variables=meta'
        WHEN NULLIF(superage.canon_source(sub_source), 'Taboola') = 'Meta'
            THEN 'L3 sub_source: ' || LOWER(TRIM(sub_source))
        WHEN NULLIF(superage.canon_source(source), 'Taboola') = 'Meta'
            THEN 'L4 source: ' || LOWER(TRIM(source))
        WHEN NULLIF(superage.canon_source(utm_source), 'Taboola') = 'Meta'
            THEN 'L5 utm_source: ' || LOWER(TRIM(utm_source))
        ELSE '(other)'
    END AS meta_driver,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Meta'
GROUP BY 1 ORDER BY subs DESC;


-- 3. Bug's contribution in one number: Meta subs whose ONLY Meta signal is a
--    campaign name (fitness_power_quiz / longivity_quiz / longevity_quiz /
--    fitness_quiz) in sub_source/source/utm_source, with NO plain
--    facebook/ig/meta/fb signal — i.e. precisely what the old code missed.
SELECT
    COUNT(*) AS meta_subs_only_caught_by_fix
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Meta'
  -- driven by a campaign-name value somewhere in the chain …
  AND (
        LOWER(TRIM(acquisition_utm_source)) IN ('fitness_power_quiz','longivity_quiz','longevity_quiz','fitness_quiz')
     OR LOWER(TRIM(sub_source))             IN ('fitness_power_quiz','longivity_quiz','longevity_quiz','fitness_quiz')
     OR LOWER(TRIM(source))                 IN ('fitness_power_quiz','longivity_quiz','longevity_quiz','fitness_quiz')
     OR LOWER(TRIM(utm_source))             IN ('fitness_power_quiz','longivity_quiz','longevity_quiz','fitness_quiz')
      )
  -- … and NOT already obviously Meta via a plain facebook/ig signal
  AND LOWER(TRIM(acquisition_utm_source)) NOT IN ('facebook','meta','fb','ig')
  AND LOWER(TRIM(sub_source))             NOT IN ('facebook','meta','fb','ig')
  AND LOWER(TRIM(source))                 NOT IN ('facebook','meta','fb','ig')
  AND LOWER(TRIM(utm_source))             NOT IN ('facebook','meta','fb','ig');


-- 4. What's genuinely still "other brands" (the corrected 212) — by source
SELECT source_label, COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label NOT IN ('Taboola','Meta','Organic')
  AND source_label IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;
