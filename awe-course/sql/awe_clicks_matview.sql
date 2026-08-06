-- =============================================================================
-- Materialized View: superage.mv_awe_clicks
-- =============================================================================
-- Pre-aggregates AWE-course CAMPAIGN clicks (contact activity) into a tiny
-- per-(brand, email, campaign, day) rollup the awe_metrics Lambda reads instead
-- of scanning the raw tables. Four brands now: SuperAge, HealthBrief, AllHealthy,
-- Ageist.
--
-- Per-source floor (loss-free; AWE clicks can't predate launch), and the small
-- click-only tables keep the build fast:
--     SuperAge     superage."Campaigns_Clicks"        from 2026-07-01
--     HealthBrief  optimism.healthbrief_clicks        from 2026-07-27
--     AllHealthy   optimism.allhealthy_clicks         from 2026-07-27
--     Ageist       ageist.ageist_clicks               from 2026-07-01
--
-- Weighting: SuperAge/HealthBrief/AllHealthy have ONE row per click event (w=1);
-- Ageist rows are pre-aggregated per member/link, so w = click_count. The rollup
-- keeps click_count = SUM(w) so non-unique totals are correct for every brand;
-- unique clickers = COUNT(DISTINCT email) FILTER (WHERE email <> '').
--
-- Keep the URL pattern in sync with the Lambda's AWE_URL_PATTERNS.
-- Apply:  psql "$DATABASE_URL" -f awe_clicks_matview.sql
-- Rebuild: DROP MATERIALIZED VIEW IF EXISTS superage.mv_awe_clicks CASCADE;  then re-run.
-- =============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS superage.mv_awe_clicks AS
SELECT
    brand,
    COALESCE(email, '')    AS email,
    COALESCE(campaign, '') AS campaign,
    click_date,
    SUM(w)                 AS click_count
FROM (
    -- SuperAge (floor 2026-07-01) — one row per click event
    SELECT 'SuperAge'::text                    AS brand,
           LOWER(TRIM("EmailAddress "::text))   AS email,
           issue_name::text                     AS campaign,
           "Date"::date                         AS click_date,
           1::bigint                            AS w
    FROM superage."Campaigns_Clicks"
    WHERE "URL" LIKE '%superage.com/awecourse%'
      AND "Date" >= DATE '2026-07-01'

    UNION ALL

    -- HealthBrief (floor 2026-07-27) — one row per click event
    SELECT 'HealthBrief', LOWER(TRIM(email::text)), mailing_name::text, "timestamp"::date, 1::bigint
    FROM optimism.healthbrief_clicks
    WHERE type = 'click' AND bot = 'No'
      AND data LIKE '%superage.com/awecourse%'
      AND "timestamp" >= DATE '2026-07-27'

    UNION ALL

    -- AllHealthy (floor 2026-07-27) — one row per click event
    SELECT 'AllHealthy', LOWER(TRIM(email::text)), mailing_name::text, "timestamp"::date, 1::bigint
    FROM optimism.allhealthy_clicks
    WHERE type = 'click' AND bot = 'No'
      AND data LIKE '%superage.com/awecourse%'
      AND "timestamp" >= DATE '2026-07-27'

    UNION ALL

    -- Ageist (floor 2026-07-01) — pre-aggregated: w = click_count
    SELECT 'Ageist', LOWER(TRIM(email_address::text)), campaign_title::text,
           first_seen_at::date, COALESCE(click_count, 1)::bigint
    FROM ageist.ageist_clicks
    WHERE final_url LIKE '%awecourse%'
      AND first_seen_at >= DATE '2026-07-01'
) s
GROUP BY brand, COALESCE(email,''), COALESCE(campaign,''), click_date;

-- Unique index (no NULLs) — REQUIRED for REFRESH ... CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_awe_clicks
    ON superage.mv_awe_clicks (brand, email, campaign, click_date);
CREATE INDEX IF NOT EXISTS idx_mv_awe_clicks_email ON superage.mv_awe_clicks (email);
CREATE INDEX IF NOT EXISTS idx_mv_awe_clicks_date  ON superage.mv_awe_clicks (click_date);

-- Daily refresh at 12:30 UTC (CONCURRENTLY = readers not blocked). Requires pg_cron.
SELECT cron.schedule(
    'refresh-mv-awe-clicks',
    '30 12 * * *',
    $$ REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_awe_clicks $$
);

-- Manual refresh:  REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_awe_clicks;
-- Unschedule:      SELECT cron.unschedule('refresh-mv-awe-clicks');
