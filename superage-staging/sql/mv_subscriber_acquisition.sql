-- =============================================================================
-- Materialized View: superage.mv_subscriber_acquisition
-- =============================================================================
-- Purpose
-- -------
-- Single source of truth for a subscriber's canonical acquisition source.
--
-- The 5-level attribution chain used to live copy-pasted in FOUR places
-- (superage_metrics_lambda_updated.py `_canon_source`/`_priority_source`,
-- superage_comparison_lambda.py inline CASE blocks, superage_slack_report_lambda.py
-- bucket CASE, and utmLabel() in index.html) — each of which had to be edited
-- by hand and kept in sync. This MV computes the chain ONCE so every consumer
-- can just JOIN and read `source_label`.
--
-- The chain (identical to what the lambdas produce today, so swapping to this
-- MV does NOT move any dashboard numbers):
--   L1  acquisition_utm_source            (subscriber_acquisition; Taboola trusted here only)
--   L2  url_variables utm_source=meta      (Meta only, gated to date_subscribed >= 2025-11-01)
--   L3  sub_source                         (Taboola dropped)
--   L4  source                             (Taboola dropped)
--   L5  utm_source                         (Taboola dropped)
--   →   'Organic'                          (fallback)
-- The per-value raw→canonical mapping (ahcpl1→AllHealthy, facebook→Meta, …)
-- lives in ONE place: the superage.canon_source() function below.
--
-- Columns
-- -------
--   email                    TEXT  — lower-cased, trimmed; PRIMARY grain (unique)
--   date_joined              -- as-is from subscribers
--   date_subscribed          -- as-is from subscribers
--   state                    -- as-is from subscribers
--   o_event                  TEXT  — raw o_event (used for the Meta split column)
--   acquisition_utm_source   -- winning subscriber_acquisition row (latest acquisition_date)
--   acquisition_date         DATE  — that row's acquisition_date (America/Denver)
--   sub_source, source, utm_source, url_variables  -- raw chain inputs (debugging)
--   source_label             TEXT  — the canonical source (EXACT current chain output)
--   source_label_meta_split  TEXT  — same, but Meta is split by o_event into
--                                    'Meta - Longevity Quiz' / 'Meta - Fitness Quiz' / 'Meta'
--                                    (opt-in; not used until a consumer chooses it)
--
-- One row per email. subscriber_acquisition is de-duped with DISTINCT ON
-- (latest acquisition_date); subscribers is de-duped with DISTINCT ON (earliest
-- date_joined) so the unique index — required by REFRESH CONCURRENTLY — holds.
--
-- Refresh schedule
-- ----------------
-- Daily at 03:15 UTC via pg_cron (after mv_opens_daily's 03:00 job, before the
-- dashboard lambdas run). CONCURRENTLY = reads are never blocked; the old
-- snapshot stays live until the new one is swapped in.
--
-- How to apply this file
-- ----------------------
--   psql "$DATABASE_URL" -f mv_subscriber_acquisition.sql
--
-- To rebuild from scratch (drops indexes too — the CREATEs below re-add them):
--   DROP MATERIALIZED VIEW IF EXISTS superage.mv_subscriber_acquisition CASCADE;
--
-- Manual refresh (e.g. after a backfill):
--   REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_subscriber_acquisition;
--
-- Inspect / remove the cron job:
--   SELECT * FROM cron.job;
--   SELECT cron.unschedule('refresh-mv-subscriber-acquisition');
-- =============================================================================


-- Step 0: Canonical source mapping function
-- -----------------------------------------
-- Pure value→label lookup, mirrors _canon_source() in the metrics lambda.
-- IMMUTABLE + no I/O so Postgres can inline it. Returns NULL for empty /
-- placeholder inputs so the COALESCE chain falls through to the next level.
-- NOTE: chain-level rules (Taboola gating, the url_variables Meta gate) are
-- NOT here — they live in the view, because they depend on which level we are
-- at, not on the value alone.

CREATE OR REPLACE FUNCTION superage.canon_source(col text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        -- Empty / placeholder AND quiz/o_event tokens → NULL so the chain keeps
        -- going. Quiz names (fitness_power_quiz, longevity_quiz, …) are NOT
        -- acquisition sources — they must not short-circuit the chain or force a
        -- label. A quiz sub only becomes Meta if it actually reaches a real Meta
        -- signal (facebook/ig, url_variables utm_source=meta) further down.
        WHEN LOWER(TRIM(col)) IN ('none','null','(none)','(null)','-','n/a',
                                  'fitness_power_quiz','fitness_quiz',
                                  'longivity_quiz','longevity_quiz')            THEN NULL
        WHEN LOWER(TRIM(col)) IN ('organic','direct')                                    THEN 'Organic'
        WHEN LOWER(TRIM(col)) IN ('website','homepage','home','web','site','games_website') THEN 'Website'
        WHEN LOWER(TRIM(col)) IN ('ahcpl1','allhealthy','allhealthy.com')                THEN 'AllHealthy'
        WHEN LOWER(TRIM(col)) = 'tdcpl1'                                                 THEN 'TDCPL'
        WHEN LOWER(TRIM(col)) = 'tdcpl2'                                                 THEN 'TDCPL'
        WHEN LOWER(TRIM(col)) LIKE 'td_cpl2%'                                            THEN 'TDCPL'
        WHEN LOWER(TRIM(col)) IN ('lscpl1','lscpl2','ls_cpl2','livingsimply','livingsimply.com') THEN 'LSCPL'
        -- Meta = genuine Meta signals only (facebook / instagram). Quiz campaign
        -- names are handled above (pass-through) — the Meta decision comes from
        -- an actual Meta value in the chain, never from a quiz name.
        WHEN LOWER(TRIM(col)) IN ('facebook','meta','fb','ig')                           THEN 'Meta'
        WHEN LOWER(TRIM(col)) IN ('if','ifcpl1')                                         THEN 'IFCPL'
        WHEN LOWER(TRIM(col)) = 'taboola'                                                THEN 'Taboola'
        WHEN LOWER(TRIM(col)) = 'healthbrief'                                            THEN 'HealthBrief'
        WHEN LOWER(TRIM(col)) IN ('superagequiz')                                        THEN 'SuperAge Quiz'
        WHEN LOWER(TRIM(col)) IN ('theageist','theageist001','ageist')                   THEN 'TheAgeist'
        WHEN LOWER(TRIM(col)) LIKE 'ageist_%'                                            THEN 'TheAgeist'
        WHEN LOWER(TRIM(col)) LIKE 'ageistrequest%'                                      THEN 'TheAgeist'
        WHEN LOWER(TRIM(col)) IN ('recommendedreads.com','rr_cpl2')                      THEN 'RRCPL'
        WHEN LOWER(TRIM(col)) LIKE 'rrcpl1%'                                             THEN 'RRCPL'
        WHEN LOWER(TRIM(col)) = 'campaign_monitor'                                       THEN 'Campaign Monitor'
        WHEN LOWER(TRIM(col)) IN ('welcome flow','welcome+flow')                         THEN 'Welcome Flow'
        WHEN LOWER(TRIM(col)) = 'nncpl1'                                                 THEN 'NNCPL'
        WHEN LOWER(TRIM(col)) LIKE 'nn_cpl2%'                                            THEN 'NNCPL'
        WHEN LOWER(TRIM(col)) LIKE 'nn1_cpl2%'                                           THEN 'NNCPL'
        WHEN LOWER(TRIM(col)) IN ('is','iscpl1')                                         THEN 'ISCPL'
        WHEN LOWER(TRIM(col)) IN ('chatgpt.com','perplexity','nbot.ai')                  THEN 'AI'
        WHEN LOWER(TRIM(col)) = 'refind'                                                 THEN 'Refind'
        WHEN LOWER(TRIM(col)) = 'superage'                                               THEN 'SuperAge'
        ELSE NULLIF(TRIM(col), '')
    END
$$;


-- Step 1: Create the materialized view
-- -------------------------------------
DROP MATERIALIZED VIEW IF EXISTS superage.mv_subscriber_acquisition CASCADE;

CREATE MATERIALIZED VIEW superage.mv_subscriber_acquisition AS
WITH sa_acq AS (
    -- Winning acquisition row per email: latest acquisition_date wins.
    SELECT DISTINCT ON (LOWER(TRIM(email)))
        LOWER(TRIM(email))                                                          AS email,
        acquisition_utm_source,
        (acquisition_date AT TIME ZONE 'UTC' AT TIME ZONE 'America/Denver')::date   AS acquisition_date
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added', 'resubscribed')
    ORDER BY LOWER(TRIM(email)), acquisition_date DESC NULLS LAST
),
sub AS (
    -- One row per email from subscribers: earliest date_joined wins.
    SELECT DISTINCT ON (LOWER(TRIM(email)))
        LOWER(TRIM(email))  AS email,
        date_joined,
        date_subscribed,
        state,
        o_event,
        sub_source,
        source,
        utm_source,
        url_variables
    FROM superage.subscribers
    WHERE email IS NOT NULL AND TRIM(email) <> ''
    ORDER BY LOWER(TRIM(email)), date_joined ASC NULLS LAST
),
chained AS (
    SELECT
        s.email,
        s.date_joined,
        s.date_subscribed,
        s.state,
        s.o_event,
        sa.acquisition_utm_source,
        sa.acquisition_date,
        s.sub_source,
        s.source,
        s.utm_source,
        s.url_variables,
        -- The 5-level chain. no_taboola() is inlined per level: a Taboola match
        -- below L1 is discarded (acquisition_utm_source is the only trusted
        -- Taboola signal), letting the chain fall through.
        COALESCE(
            -- L1: acquisition_utm_source (Taboola allowed)
            superage.canon_source(sa.acquisition_utm_source),
            -- L2: url_variables utm_source=meta, gated to 2025-11-01+
            CASE
                WHEN LOWER(TRIM(SUBSTRING(s.url_variables FROM 'utm_source=([^,&]+)'))) = 'meta'
                 AND COALESCE(s.date_subscribed, s.date_joined)::date >= '2025-11-01'
                THEN 'Meta' ELSE NULL
            END,
            -- L3: sub_source (Taboola dropped)
            NULLIF(superage.canon_source(s.sub_source), 'Taboola'),
            -- L4: source (Taboola dropped)
            NULLIF(superage.canon_source(s.source), 'Taboola'),
            -- L5: utm_source (Taboola dropped)
            NULLIF(superage.canon_source(s.utm_source), 'Taboola'),
            'Organic'
        ) AS source_label
    FROM sub s
    LEFT JOIN sa_acq sa ON sa.email = s.email
)
SELECT
    email,
    date_joined,
    date_subscribed,
    state,
    o_event,
    acquisition_utm_source,
    acquisition_date,
    sub_source,
    source,
    utm_source,
    url_variables,
    source_label,
    -- Opt-in Meta split by o_event. Base label unchanged for every other source.
    CASE
        WHEN source_label = 'Meta' THEN
            CASE
                WHEN LOWER(TRIM(o_event)) IN ('longivity_quiz', 'longevity_quiz') THEN 'Meta - Longevity Quiz'
                WHEN LOWER(TRIM(o_event)) = 'fitness_quiz'                         THEN 'Meta - Fitness Quiz'
                ELSE 'Meta'
            END
        ELSE source_label
    END AS source_label_meta_split
FROM chained;


-- Step 2: Indexes
-- ----------------
-- Unique index is REQUIRED for REFRESH CONCURRENTLY.
CREATE UNIQUE INDEX ON superage.mv_subscriber_acquisition (email);

-- GROUP BY source_label (the dominant dashboard aggregation).
CREATE INDEX ON superage.mv_subscriber_acquisition (source_label);

-- Range scans for weekly/monthly windows.
CREATE INDEX ON superage.mv_subscriber_acquisition (date_joined);
CREATE INDEX ON superage.mv_subscriber_acquisition (date_subscribed);


-- Step 3: Schedule daily refresh via pg_cron
-- -------------------------------------------
-- '15 3 * * *' = every day at 03:15 UTC — after mv_opens_daily (03:00), before
-- the dashboard lambdas. Re-calling with the same job name replaces the schedule.
SELECT cron.schedule(
    'refresh-mv-subscriber-acquisition',
    '15 3 * * *',
    $$ REFRESH MATERIALIZED VIEW CONCURRENTLY superage.mv_subscriber_acquisition $$
);


-- =============================================================================
-- Verification (run manually after CREATE; NOT part of the migration)
-- =============================================================================
-- These should match the current dashboard's "acquisition source" numbers.
-- If a bucket is off, the chain in the MV has drifted from the lambda.
--
--   -- All-time source distribution (compare to metrics lambda acquisition rows):
--   SELECT source_label, COUNT(*) AS subs
--   FROM superage.mv_subscriber_acquisition
--   GROUP BY 1 ORDER BY 2 DESC;
--
--   -- Meta split sanity check:
--   SELECT source_label_meta_split, COUNT(*)
--   FROM superage.mv_subscriber_acquisition
--   WHERE source_label = 'Meta'
--   GROUP BY 1 ORDER BY 2 DESC;
--
--   -- Row count must equal distinct subscriber emails:
--   SELECT
--     (SELECT COUNT(*) FROM superage.mv_subscriber_acquisition)              AS mv_rows,
--     (SELECT COUNT(DISTINCT LOWER(TRIM(email)))
--        FROM superage.subscribers
--        WHERE email IS NOT NULL AND TRIM(email) <> '')                      AS distinct_emails;
-- =============================================================================
