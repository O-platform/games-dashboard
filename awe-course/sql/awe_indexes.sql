-- =============================================================================
-- AWE Course — performance indexes for the metrics lambda
-- =============================================================================
-- Diagnosis (from sql/awe_diagnostics.sql on the live DB):
--   • allhealthy_contact_activity  = 248M rows / 75 GB
--   • healthbrief_contact_activity =  60M rows / 34 GB
--   • Campaigns_Clicks             = 2.2M rows
--   The existing (timestamp,type) indexes ARE used, but still leave millions of
--   recent "click" rows whose `data` must be tested against the AWE `ILIKE` one
--   by one (data/URL is not indexed) — that's the multi-minute / timeout cost.
--
-- FIX: a PARTIAL index containing ONLY rows that match the AWE course URL. The
-- predicate is immutable, so Postgres indexes just those rows — the index is
-- essentially empty today and stays tiny. Queries then hit ~0 rows instantly.
--
-- IMPORTANT: the ILIKE literal below MUST exactly equal the lambda's
-- AWE_URL_PATTERNS (default '%superage.com/awecourse%'). The lambda inlines that
-- same literal so the planner can match this partial index. If you change
-- AWE_URL_PATTERNS, rebuild these indexes with the new literal.
--
-- CREATE INDEX CONCURRENTLY does a ONE-TIME full scan to build (minutes on the
-- big tables) but takes NO write lock. Run outside a transaction (psql default).
-- =============================================================================

-- SuperAge — Campaigns_Clicks (URL match, no type column)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sac_awecourse
    ON superage."Campaigns_Clicks" ("Date")
    WHERE "URL" ILIKE '%superage.com/awecourse%';

-- HealthBrief — click rows matching the AWE URL
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_hb_awecourse
    ON optimism.healthbrief_contact_activity ("timestamp")
    WHERE type = 'click' AND data ILIKE '%superage.com/awecourse%';

-- AllHealthy — click rows matching the AWE URL (the big one; ~one-time scan)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ah_awecourse
    ON optimism.allhealthy_contact_activity ("timestamp")
    WHERE type = 'click' AND data ILIKE '%superage.com/awecourse%';

-- Verify the planner uses them (should show "Index Scan using idx_..._awecourse",
-- not "Seq Scan" / a scan over the whole (timestamp,type) range):
--   EXPLAIN SELECT COUNT(*) FROM optimism.allhealthy_contact_activity
--   WHERE "timestamp" >= DATE '2026-07-01'
--     AND data ILIKE '%superage.com/awecourse%' AND type='click' AND bot='No';


-- ─────────────────────────────────────────────────────────────
-- ALTERNATIVE (only if you need arbitrary/changing URL patterns): pg_trgm GIN.
-- Works with any pattern at query time, but the index is LARGE (GBs on these
-- tables) and slow to build. pg_trgm is already installed (v1.6). Prefer the
-- partial indexes above unless the match pattern must vary.
-- ─────────────────────────────────────────────────────────────
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ah_data_trgm
--     ON optimism.allhealthy_contact_activity USING gin (data gin_trgm_ops)
--     WHERE type = 'click';
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_hb_data_trgm
--     ON optimism.healthbrief_contact_activity USING gin (data gin_trgm_ops)
--     WHERE type = 'click';
