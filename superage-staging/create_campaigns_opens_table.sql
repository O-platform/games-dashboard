-- ============================================================
-- Campaigns_Opens — raw open events (one row per open event)
-- Mirrors the existing Campaigns_Clicks table structure.
-- Run once against your RDS instance before the first Lambda run.
-- ============================================================

CREATE TABLE IF NOT EXISTS superage."Campaigns_Opens" (
    id            BIGSERIAL    PRIMARY KEY,
    email         VARCHAR(320) NOT NULL,          -- lowercased + trimmed
    campaign_id   BIGINT,                         -- Ongage message/campaign id
    campaign_name TEXT,
    opened_at     TIMESTAMPTZ  NOT NULL,           -- UTC open timestamp
    list_id       VARCHAR(64),                     -- Ongage list id
    created_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- Unique constraint — prevents duplicate rows on re-runs / re-ingestion
ALTER TABLE superage."Campaigns_Opens"
    ADD CONSTRAINT uq_camps_opens
    UNIQUE (email, campaign_id, opened_at);

-- Indexes — cover the most common query patterns
CREATE INDEX IF NOT EXISTS idx_camps_opens_email
    ON superage."Campaigns_Opens" (email);

CREATE INDEX IF NOT EXISTS idx_camps_opens_opened
    ON superage."Campaigns_Opens" (opened_at);

CREATE INDEX IF NOT EXISTS idx_camps_opens_campaign
    ON superage."Campaigns_Opens" (campaign_id);

-- Partial index for rolling-window queries (last 120 days)
CREATE INDEX IF NOT EXISTS idx_camps_opens_recent
    ON superage."Campaigns_Opens" (email, opened_at)
    WHERE opened_at >= NOW() - INTERVAL '120 days';


-- ============================================================
-- Useful queries once data is loaded
-- ============================================================

-- Weekly distinct openers (rolling 30-day window) — last 13 weeks
-- (equivalent of the "openers-30" metric requested)
SELECT
    week_end,
    COUNT(DISTINCT email) AS openers_rolling_30d
FROM (
    SELECT
        gs::date AS week_end
    FROM generate_series(
        CURRENT_DATE - INTERVAL '12 weeks',
        CURRENT_DATE,
        INTERVAL '1 week'
    ) gs
) weeks
JOIN superage."Campaigns_Opens" o
    ON o.opened_at::date BETWEEN weeks.week_end - 29 AND weeks.week_end
GROUP BY 1
ORDER BY 1;


-- Weekly unique openers (opened in that specific week only)
SELECT
    DATE_TRUNC('week', opened_at)::date AS week_start,
    COUNT(DISTINCT email)               AS unique_openers,
    COUNT(*)                            AS total_opens,
    COUNT(DISTINCT campaign_id)         AS campaigns_with_opens
FROM superage."Campaigns_Opens"
WHERE opened_at >= CURRENT_DATE - INTERVAL '13 weeks'
GROUP BY 1
ORDER BY 1 DESC;


-- Weekly avg open rate (opens / recipients) — join with Campaigns table
SELECT
    DATE_TRUNC('week', c."Sent Date ")::date AS week_start,
    SUM(c."UniqueOpened")                    AS unique_opens,
    SUM(c."Recipients")                      AS recipients,
    ROUND(
        SUM(c."UniqueOpened")::numeric
          / NULLIF(SUM(c."Recipients"), 0)
          * 100,
        2
    ) AS weighted_open_rate_pct
FROM superage."Campaigns" c
WHERE c."Sent Date " IS NOT NULL
  AND c."Sent Date "::date >= CURRENT_DATE - INTERVAL '13 weeks'
  AND c."Recipients" > 95
  AND EXTRACT(DOW FROM c."Sent Date "::date) <> 0   -- exclude Sunday
GROUP BY 1
ORDER BY 1 DESC;
