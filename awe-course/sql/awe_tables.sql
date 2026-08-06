-- =============================================================================
-- AWE Course dashboard — table DDL  (schema: superage)
-- =============================================================================
-- Apply once against the main RDS instance (the same DB that hosts
-- superage.*, optimism.* and public.*):
--
--   psql "$DATABASE_URL" -f awe_tables.sql
--
-- Only ONE table is created for now:
--   • superage.awe_waitlist  — populated by awe_waitlist_ingest_lambda from the
--     Campaign Monitor "NSR" list (full refresh each run).
--
-- Buyers are NOT created here — they come from the Circle sync table
-- superage.awe_course_members (managed externally). awe_metrics_lambda reads it
-- via AWE_MEMBERS_TABLE for the buyers funnel stage, revenue, and persona.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS superage;

-- ─────────────────────────────────────────────────────────────
-- superage.awe_waitlist
-- ─────────────────────────────────────────────────────────────
-- One row per Campaign Monitor subscriber on the AWE waitlist list.
-- Loaded via TRUNCATE + bulk INSERT on every ingest run (full refresh),
-- so there is no need for an ON CONFLICT clause — but email is the natural
-- key and is kept UNIQUE to catch accidental dupes inside a single load.
--
-- The eight CM custom fields are stored BOTH as typed columns (for fast
-- GROUP BY in the dashboard's UTM breakdowns) AND, in full, inside the
-- custom_fields JSONB blob (so new CM fields are never silently dropped).
CREATE TABLE IF NOT EXISTS superage.awe_waitlist (
    email               TEXT PRIMARY KEY,          -- lower/trimmed EmailAddress
    name                TEXT,                       -- CM "Name"
    date_joined         TIMESTAMPTZ,                -- CM "ListJoinedDate" (first joined the list)
    date_subscribed     TIMESTAMPTZ,                -- CM "Date" for active rows ("Active since")
    date_unsubscribed   TIMESTAMPTZ,                -- CM "Date" for unsubscribed rows
    state               TEXT,                       -- Active / Unsubscribed / Bounced / ...

    -- CM custom fields (also mirrored in custom_fields JSONB below)
    sub_level           TEXT,
    oid                 TEXT,
    hashed_email        TEXT,
    source              TEXT,                       -- captured, but NOT charted (per spec)
    utm_source          TEXT,
    utm_medium          TEXT,
    utm_campaign        TEXT,
    o_event             TEXT,

    custom_fields       JSONB,                      -- full CM CustomFields payload
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Breakdown filters used by the dashboard (utm_source / medium / campaign).
CREATE INDEX IF NOT EXISTS idx_awe_waitlist_utm_source   ON superage.awe_waitlist (utm_source);
CREATE INDEX IF NOT EXISTS idx_awe_waitlist_utm_medium   ON superage.awe_waitlist (utm_medium);
CREATE INDEX IF NOT EXISTS idx_awe_waitlist_utm_campaign ON superage.awe_waitlist (utm_campaign);
CREATE INDEX IF NOT EXISTS idx_awe_waitlist_state        ON superage.awe_waitlist (state);
CREATE INDEX IF NOT EXISTS idx_awe_waitlist_subscribed   ON superage.awe_waitlist (date_subscribed);

-- Note on date_joined vs date_subscribed:
-- Campaign Monitor returns TWO dates per subscriber and they can differ:
--   date_joined     <- ListJoinedDate  (when they first joined the list)
--   date_subscribed <- Date            (last state change / "Active since")
-- e.g. a subscriber can be "Joined via API on 22 Jul" but "Active since 23 Jul".
