-- ============================================================
-- superage.campaign_opens
-- Raw Campaign Monitor open events — one row per unique opener
-- per campaign (CM returns first open only, not every open event).
-- Run once against RDS before the first Lambda invocation.
-- ============================================================

CREATE TABLE IF NOT EXISTS superage.campaign_opens (
    id                 BIGSERIAL        PRIMARY KEY,
    email_address      VARCHAR(320)     NOT NULL,
    list_id            VARCHAR(100),
    opened_at          TIMESTAMPTZ,
    ip_address         VARCHAR(50),
    latitude           DOUBLE PRECISION,
    longitude          DOUBLE PRECISION,
    city               VARCHAR(100),
    region             VARCHAR(100),
    country_code       VARCHAR(10),
    country_name       VARCHAR(100),
    campaign_id        VARCHAR(100)     NOT NULL,
    campaign_name      TEXT,
    campaign_sent_date DATE,
    created_at         TIMESTAMPTZ      DEFAULT NOW(),

    -- One row per (opener, campaign). CM only returns first open per person.
    CONSTRAINT uq_campaign_opens UNIQUE (email_address, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_camp_opens_email
    ON superage.campaign_opens (email_address);

CREATE INDEX IF NOT EXISTS idx_camp_opens_opened
    ON superage.campaign_opens (opened_at);

CREATE INDEX IF NOT EXISTS idx_camp_opens_campaign
    ON superage.campaign_opens (campaign_id);

-- Partial index for rolling-window queries (most common access pattern)
CREATE INDEX IF NOT EXISTS idx_camp_opens_recent
    ON superage.campaign_opens (email_address, opened_at)
    WHERE opened_at >= NOW() - INTERVAL '120 days';


-- ============================================================
-- Useful queries once data is loaded
-- ============================================================

-- Distinct openers rolling 30d — one point per week (last 13 weeks)
-- This is the "openers-30" weekly trendline
SELECT
    week_end::date,
    COUNT(DISTINCT email_address) AS unique_openers_rolling_30d
FROM
    generate_series(
        CURRENT_DATE - INTERVAL '12 weeks',
        CURRENT_DATE,
        INTERVAL '1 week'
    ) AS week_end
JOIN superage.campaign_opens o
    ON o.opened_at::date BETWEEN week_end::date - 29 AND week_end::date
GROUP BY 1
ORDER BY 1;


-- Weekly unique openers (opened in that specific ISO week)
SELECT
    DATE_TRUNC('week', opened_at)::date AS week_start,
    COUNT(DISTINCT email_address)       AS unique_openers,
    COUNT(*)                            AS total_opens,
    COUNT(DISTINCT campaign_id)         AS campaigns
FROM superage.campaign_opens
WHERE opened_at >= CURRENT_DATE - INTERVAL '13 weeks'
GROUP BY 1
ORDER BY 1 DESC;


-- Weekly avg open rate — join with Campaigns for recipients denominator
SELECT
    DATE_TRUNC('week', c."Sent Date ")::date AS week_start,
    COUNT(DISTINCT o.email_address)          AS unique_openers,
    SUM(c."Recipients")                      AS recipients,
    ROUND(
        COUNT(DISTINCT o.email_address)::numeric
          / NULLIF(SUM(c."Recipients"), 0) * 100,
        2
    ) AS open_rate_pct
FROM superage."Campaigns" c
LEFT JOIN superage.campaign_opens o USING (campaign_id)
WHERE c."Sent Date " >= CURRENT_DATE - INTERVAL '13 weeks'
  AND c."Recipients" > 95
  AND EXTRACT(DOW FROM c."Sent Date "::date) <> 0   -- exclude Sundays
GROUP BY 1
ORDER BY 1 DESC;
