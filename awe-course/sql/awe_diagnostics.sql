-- =============================================================================
-- AWE Course — diagnostics for the slow AH/HB click scans
-- =============================================================================
-- Run these to understand WHY awe_metrics is slow on the contact-activity tables
-- and decide which index in awe_indexes.sql to build. Read-only; safe to run.
-- =============================================================================

-- 1) What indexes already exist on the three click tables?
--    (Look for any index on the date column: "Date" / "timestamp".)
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE (schemaname = 'optimism' AND tablename IN ('healthbrief_contact_activity','allhealthy_contact_activity'))
   OR (schemaname = 'superage' AND tablename = 'Campaigns_Clicks')
ORDER BY schemaname, tablename, indexname;

-- 2) How big are they (size + estimated row count)?
SELECT c.oid::regclass                                  AS table_name,
       pg_size_pretty(pg_total_relation_size(c.oid))    AS total_size,
       pg_size_pretty(pg_relation_size(c.oid))          AS heap_size,
       to_char(c.reltuples, 'FM999,999,999,999')        AS est_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname = 'optimism' AND c.relname IN ('healthbrief_contact_activity','allhealthy_contact_activity'))
   OR (n.nspname = 'superage' AND c.relname = 'Campaigns_Clicks');

-- 3) Column types (confirm the date columns are real timestamps, and data is text)
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE (table_schema, table_name) IN (
        ('optimism','healthbrief_contact_activity'),
        ('optimism','allhealthy_contact_activity'),
        ('superage','Campaigns_Clicks'))
ORDER BY table_schema, table_name, ordinal_position;

-- 4) Is pg_trgm available / already installed? (needed for Option B)
SELECT extname AS installed FROM pg_extension WHERE extname = 'pg_trgm';
SELECT name, default_version FROM pg_available_extensions WHERE name = 'pg_trgm';

-- 5) THE KEY CHECK — the query plan (instant; does NOT run the scan).
--    "Seq Scan" on the whole table = the problem. "Index Scan" / "Bitmap Index
--    Scan" using the date or a trigram index = fixed. Run after building indexes
--    to confirm the planner actually uses them.
EXPLAIN
SELECT COUNT(*) FROM optimism.allhealthy_contact_activity
WHERE "timestamp" >= DATE '2026-07-01'
  AND data ILIKE '%superage.com/awecourse%'
  AND type = 'click' AND bot = 'No';

EXPLAIN
SELECT COUNT(*) FROM optimism.healthbrief_contact_activity
WHERE "timestamp" >= DATE '2026-07-01'
  AND data ILIKE '%superage.com/awecourse%'
  AND type = 'click' AND bot = 'No';

EXPLAIN
SELECT COUNT(*) FROM superage."Campaigns_Clicks"
WHERE "Date" >= DATE '2026-07-01'
  AND "URL" ILIKE '%superage.com/awecourse%';

-- 6) OPTIONAL — real timing incl. buffers. WARNING: this actually runs the scan
--    and can take minutes on the big tables. Uncomment to measure.
-- EXPLAIN (ANALYZE, BUFFERS)
-- SELECT COUNT(*) FROM optimism.allhealthy_contact_activity
-- WHERE "timestamp" >= DATE '2026-07-01'
--   AND data ILIKE '%superage.com/awecourse%'
--   AND type = 'click' AND bot = 'No';
