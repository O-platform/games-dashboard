-- =============================================================================
-- Materialized View: superage.mv_opens_daily
-- =============================================================================
-- Purpose
-- -------
-- Pre-aggregates optimism.superage_opens (78M+ rows) into deduplicated
-- (open_date, email, campaign_id) tuples. Queries that need unique-opener
-- counts hit this ~5-10M row view instead of scanning the raw table — turning
-- 800-second scans into sub-second lookups.
--
-- Schema note
-- -----------
-- The raw table lives in the `optimism` schema.
-- The view lives in the `superage` schema alongside the rest of the dashboard
-- objects so permissions and search_path stay consistent.
--
-- Columns
-- -------
--   open_date   DATE     — truncated calendar date of the open event
--   email       TEXT     — lower-cased, trimmed email address
--   campaign_id TEXT/INT — as-is from superage_opens.campaign_id
--
-- Each row is ONE unique (subscriber × date × campaign) combination. Multiple
-- open events by the same subscriber for the same campaign on the same day
-- collapse to a single row.
--
-- Indexes
-- -------
-- UNIQUE index on (open_date, email, campaign_id) enables:
--   • REFRESH MATERIALIZED VIEW CONCURRENTLY (requires a unique index)
--   • equality/range lookups on all three columns
-- Additional indexes on open_date and email accelerate the two most common
-- filter patterns used by the dashboard lambdas:
--   • WHERE open_date >= ... (range scans for weekly/monthly windows)
--   • COUNT(DISTINCT email) / GROUP BY email
--
-- Refresh schedule
-- ----------------
-- Daily at 03:00 server time via pg_cron (see cron.schedule() call below).
-- CONCURRENTLY means reads are not blocked during refresh — the old snapshot
-- stays live until the new one is swapped in.
--
-- How to apply this file
-- ----------------------
-- Run as a superuser or as the schema owner on the target RDS instance:
--
--   psql "$DATABASE_URL" -f mv_opens_daily.sql
--
-- The CREATE MATERIALIZED VIEW will fail if the view already exists — wrap in
-- DROP MATERIALIZED VIEW IF EXISTS superage.mv_opens_daily CASCADE first if you
-- need to rebuild from scratch (this will drop the indexes too; re-run the
-- CREATE INDEX statements below).
--
-- To check existing pg_cron jobs:
--   SELECT * FROM cron.job;
--
-- To remove the scheduled refresh:
--   SELECT cron.unschedule('refresh-mv-opens-daily');
--
-- To trigger a manual refresh (e.g. after backfilling superage_opens):
--   REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_opens_daily;
-- =============================================================================


-- Step 1: Create the materialized view
-- -------------------------------------
-- GROUP BY deduplicates: one row per (date, email, campaign_id) tuple.
-- LOWER(TRIM(email)) normalises the address so lookups don't depend on case.
-- Rows where email IS NULL or blank are excluded — they cannot be counted as
-- unique subscribers and would bloat the view without adding signal.

CREATE MATERIALIZED VIEW superage.mv_opens_daily AS
SELECT
    opened_at::date                AS open_date,
    LOWER(TRIM(email))             AS email,
    campaign_id
FROM optimism.superage_opens
WHERE email IS NOT NULL
  AND TRIM(email) != ''
GROUP BY 1, 2, 3;


-- Step 2: Create indexes
-- -----------------------
-- Unique index is REQUIRED for REFRESH CONCURRENTLY.
CREATE UNIQUE INDEX ON superage.mv_opens_daily (open_date, email, campaign_id);

-- Range scans: WHERE open_date >= ... AND open_date < ...
CREATE INDEX ON superage.mv_opens_daily (open_date);

-- Lookup / COUNT DISTINCT by email
CREATE INDEX ON superage.mv_opens_daily (email);


-- Step 3: Schedule daily refresh via pg_cron
-- -------------------------------------------
-- Requires the pg_cron extension to be enabled on the RDS instance.
-- To enable it (one-time, superuser): CREATE EXTENSION IF NOT EXISTS pg_cron;
--
-- The job name 'refresh-mv-opens-daily' is used as the unique key — calling
-- cron.schedule() with the same name replaces the existing schedule rather
-- than creating a duplicate.
--
-- Schedule: '0 3 * * *' = every day at 03:00 UTC.
-- Adjust the time if the DB server runs in a different timezone or if you
-- prefer a different off-peak window.

SELECT cron.schedule(
    'refresh-mv-opens-daily',
    '0 3 * * *',
    $$ REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_opens_daily $$
);
