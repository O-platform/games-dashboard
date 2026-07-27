-- =============================================================================
-- Investigation: the 580 "other brands" new subs — Week of Jul 18–24, 2026
-- =============================================================================
-- "other_brands" in the Slack weekly report = canonical source_label that is
-- NOT Taboola / Meta / Organic / unknown — i.e. the co-registration & partner
-- brands (AllHealthy, TDCPL, LSCPL, NNCPL, RRCPL, IFCPL, ISCPL, HealthBrief,
-- Website, SuperAge Quiz, TheAgeist, ...). These are attributed sources, NOT
-- organic / word-of-mouth.
--
-- Window mirrors the report exactly: date_joined in [2026-07-18, 2026-07-25),
-- state = 'active', de-duped one row per email (earliest date_joined).
-- =============================================================================


-- ─────────────────────────────────────────────────────────────
-- VERSION A — after the MV is created (fast, canonical)
-- ─────────────────────────────────────────────────────────────

-- A1. Confirm the report's buckets (should read 959 / 379 Meta / 580 other / 0 / 0)
WITH base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email)) AS email, mv.source_label,
        COALESCE(TRIM(mv.acquisition_utm_source),'') AS acq_utm,
        COALESCE(TRIM(mv.sub_source),'') AS sub_src,
        COALESCE(TRIM(mv.source),'') AS src,
        COALESCE(TRIM(mv.utm_source),'') AS utm_src,
        COALESCE(TRIM(mv.url_variables),'') AS url_vars
    FROM superage.subscribers s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-18' AND s.date_joined < '2026-07-25'
      AND s.email IS NOT NULL AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state,''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
)
SELECT
    CASE
        WHEN source_label = 'Taboola' THEN 'taboola'
        WHEN source_label = 'Meta'    THEN 'meta'
        WHEN source_label = 'Organic' OR source_label IS NULL THEN
            CASE WHEN acq_utm='' AND sub_src='' AND src='' AND utm_src='' AND url_vars=''
                 THEN 'unknown' ELSE 'organic' END
        ELSE 'other_brands'
    END AS bucket,
    COUNT(*) AS subs
FROM base GROUP BY 1 ORDER BY 2 DESC;


-- A2. THE ANSWER — break the 580 "other brands" down by canonical source
WITH base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email)) AS email, mv.source_label
    FROM superage.subscribers s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-18' AND s.date_joined < '2026-07-25'
      AND s.email IS NOT NULL AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state,''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
)
SELECT source_label, COUNT(*) AS subs
FROM base
WHERE source_label NOT IN ('Taboola','Meta','Organic')
  AND source_label IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;


-- A3. Which field + raw value drives each "other brand" sub (shows ahcpl1, tdcpl1, …)
WITH base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email)) AS email, mv.source_label,
        mv.acquisition_utm_source, mv.sub_source, mv.source, mv.utm_source
    FROM superage.subscribers s
    LEFT JOIN superage.mv_subscriber_acquisition mv ON mv.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-18' AND s.date_joined < '2026-07-25'
      AND s.email IS NOT NULL AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state,''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
)
SELECT
    source_label,
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COUNT(*) AS subs
FROM base
WHERE source_label NOT IN ('Taboola','Meta','Organic') AND source_label IS NOT NULL
GROUP BY 1,2,3,4,5
ORDER BY subs DESC
LIMIT 40;


-- ─────────────────────────────────────────────────────────────
-- VERSION B — standalone (run NOW, before the MV exists).
-- Doesn't classify; just shows the raw acquisition signal for the week's
-- active new subs so you can see the brand codes directly.
-- ─────────────────────────────────────────────────────────────

-- B1. Raw acquisition_utm_source distribution (L1 — the trusted signal)
WITH sa_acq AS (
    SELECT DISTINCT ON (LOWER(TRIM(email)))
        LOWER(TRIM(email)) AS email, acquisition_utm_source
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added','resubscribed')
    ORDER BY LOWER(TRIM(email)), acquisition_date DESC NULLS LAST
),
base AS (
    SELECT DISTINCT ON (LOWER(TRIM(s.email)))
        LOWER(TRIM(s.email)) AS email,
        COALESCE(NULLIF(TRIM(sa.acquisition_utm_source),''),'(none)') AS acq_utm,
        COALESCE(NULLIF(TRIM(s.sub_source),''),'(none)') AS sub_source,
        COALESCE(NULLIF(TRIM(s.source),''),'(none)')     AS source,
        COALESCE(NULLIF(TRIM(s.utm_source),''),'(none)') AS utm_source
    FROM superage.subscribers s
    LEFT JOIN sa_acq sa ON sa.email = LOWER(TRIM(s.email))
    WHERE s.date_joined >= '2026-07-18' AND s.date_joined < '2026-07-25'
      AND s.email IS NOT NULL AND TRIM(s.email) <> ''
      AND LOWER(TRIM(COALESCE(s.state,''))) = 'active'
    ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
)
SELECT acq_utm, sub_source, source, utm_source, COUNT(*) AS subs
FROM base
GROUP BY 1,2,3,4
ORDER BY subs DESC
LIMIT 50;
