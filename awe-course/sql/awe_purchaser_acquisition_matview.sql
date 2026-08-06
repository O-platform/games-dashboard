-- =============================================================================
-- Materialized View: superage.mv_awe_purchaser_acquisition
-- =============================================================================
-- Purpose
-- -------
-- One row per PURCHASER (buyer) with their attributed acquisition UTM, using
-- LAST-TOUCH-BEFORE-PURCHASE. A buyer can have several checkout-landing clicks
-- (e.g. one from AllHealthy and one from HealthBrief); we keep the LATEST click
-- that happened AT OR BEFORE their purchase. Clicks AFTER the purchase are
-- ignored (they didn't drive the sale). A buyer with no qualifying click is
-- kept with utm_* = 'Unknown' and attributed = false.
--
-- The awe_metrics Lambda reads THIS view (AWE_PURCHASER_MATVIEW) instead of
-- doing the join at query time — so the buyer-acquisition breakdown is a plain
-- GROUP BY on a tiny per-buyer table.
--
-- Sources / timestamps (both UTC):
--   purchase time = superage.awe_course_members.circle_created_at
--   click time    = superage.awe_course_checkout_landing_events.date
--   join key      = oid (text-normalised on both sides)
--
-- Columns
-- -------
--   email        TEXT   — buyer email (lower/trimmed) — one row per buyer
--   oid          TEXT   — buyer oid (null if unattributed)
--   attributed   BOOL   — true if a click at/before purchase was found
--   utm_source   TEXT   — attributed value, or 'Unknown'
--   utm_medium   TEXT
--   utm_campaign TEXT
--   click_date   TIMESTAMPTZ — the attributed click's date (null if Unknown)
--
-- Apply after awe_course_members + awe_course_checkout_landing_events exist:
--   psql "$DATABASE_URL" -f awe_purchaser_acquisition_matview.sql
-- Rebuild from scratch:
--   DROP MATERIALIZED VIEW IF EXISTS superage.mv_awe_purchaser_acquisition CASCADE;
-- =============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS superage.mv_awe_purchaser_acquisition AS
WITH base AS (
    -- every REAL buyer (one row per email). is_superage excludes internal/team
    -- accounts — same rule as the metrics lambda.
    SELECT DISTINCT LOWER(TRIM(email)) AS email
    FROM superage.awe_course_members
    WHERE email IS NOT NULL AND TRIM(email) <> '' AND is_superage IS NOT TRUE
),
bp AS (
    -- one purchase time per buyer (earliest membership) + their oid
    SELECT DISTINCT ON (LOWER(TRIM(email)))
           LOWER(TRIM(email))       AS email,
           LOWER(TRIM(oid::text))   AS oid,
           circle_created_at        AS purchased_at
    FROM superage.awe_course_members
    WHERE email IS NOT NULL AND TRIM(email) <> '' AND is_superage IS NOT TRUE
      AND circle_created_at IS NOT NULL
      AND NULLIF(TRIM(oid::text), '') IS NOT NULL
    ORDER BY LOWER(TRIM(email)), circle_created_at ASC
),
attributed AS (
    -- latest checkout-landing click AT/BEFORE the purchase, per buyer
    SELECT DISTINCT ON (bp.email)
           bp.email, bp.oid,
           l.utm_source, l.utm_medium, l.utm_campaign,
           l.date AS click_date
    FROM bp
    JOIN superage.awe_course_checkout_landing_events l
      ON LOWER(TRIM(l.oid::text)) = bp.oid
    WHERE l.date IS NOT NULL
      AND l.date <= bp.purchased_at          -- click at/before purchase only
      AND l.product_url like '%https://super-age.circle.so/checkout/the-power-of-awe%'
    ORDER BY bp.email, l.date DESC            -- latest such click wins
)
SELECT
    b.email,
    a.oid,
    (a.email IS NOT NULL)                                   AS attributed,
    -- Unattributed / null acquisition => treat as superage + email (a SuperAge
    -- campaign), per spec — NOT 'Unknown'.
    COALESCE(NULLIF(TRIM(a.utm_source), ''),   'superage')  AS utm_source,
    COALESCE(NULLIF(TRIM(a.utm_medium), ''),   'email')     AS utm_medium,
    COALESCE(NULLIF(TRIM(a.utm_campaign), ''), 'Unknown')   AS utm_campaign,
    a.click_date
FROM base b
LEFT JOIN attributed a ON a.email = b.email;

-- Unique index (email, no NULLs) — REQUIRED for REFRESH ... CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_awe_purchaser_acq
    ON superage.mv_awe_purchaser_acquisition (email);
CREATE INDEX IF NOT EXISTS idx_mv_awe_purchaser_acq_src
    ON superage.mv_awe_purchaser_acquisition (utm_source);

-- Daily refresh at 12:30 UTC. Requires pg_cron.
--   CREATE EXTENSION IF NOT EXISTS pg_cron;   -- one-time, superuser
-- (cron.schedule upserts by job name, so re-running this reschedules it.)
SELECT cron.schedule(
    'refresh-mv-awe-purchaser-acq',
    '30 12 * * *',
    $$ REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_awe_purchaser_acquisition $$
);

-- Manual refresh / remove schedule:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_awe_purchaser_acquisition;
--   SELECT cron.unschedule('refresh-mv-awe-purchaser-acq');
--
-- Cheap to build/refresh (members + checkout-landing are small), so no special
-- indexing on the base tables is needed here.
