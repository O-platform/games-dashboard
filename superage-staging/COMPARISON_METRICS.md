# SuperAge — Campaign Comparison Metrics
## `superage_comparison_lambda.py` → `superage-comparison.json`

---

## Purpose

Week-over-week and day-over-day campaign performance comparison for SuperAge newsletters.
Enables spotting trends and anomalies: is this Monday better or worse than last Monday? Is this week tracking above/below last week?

---

## Data Source

**Table:** `superage.\"Campaigns\"`
**Schema:** `superage` (configurable via `SA_SCHEMA` env var)

---

## Campaign Filter Rules

All queries apply these filters:

```sql
\"Sent Date \" IS NOT NULL
AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
AND \"Recipients\" > 95
```

- `> 95 recipients` — excludes test sends and internal mailings
- `<= today - 2 days` — requires campaigns to have had at least 2 days for opens/clicks to accumulate before comparison

---

## Week Definition

- **ISO week**: Monday–Sunday
- **Current week**: ISO week of the most recently launched campaign
- **Previous week**: the week immediately preceding the current week

---

## Metrics & Queries

### 1. Week-Over-Week Summary

Aggregates all qualifying campaigns in current week vs previous week.

```sql
-- Current week campaigns
SELECT
    COUNT(*)                                    AS campaigns,
    SUM(\"Recipients\")                           AS recipients,
    SUM(\"UniqueOpened\")                         AS unique_opens,
    SUM(\"Clicks\")                               AS clicks,
    SUM(\"Unsubscribed\")                         AS unsubs,
    SUM(\"Bounced\")                              AS bounced,
    ROUND(AVG(\"UOpenRate\")::numeric, 2)         AS avg_open_rate,
    ROUND(AVG(\"UClickRate\")::numeric, 2)        AS avg_click_rate,
    MAX(\"UOpenRate\")                            AS best_open_rate,
    MAX(\"UClickRate\")                           AS best_click_rate
FROM superage.\"Campaigns\"
WHERE \"Sent Date \" IS NOT NULL
  AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
  AND \"Recipients\" > 95
  AND \"Sent Date \"::date BETWEEN :cur_week_monday AND :cur_week_sunday;

-- Previous week: same query with BETWEEN :prev_week_monday AND :prev_week_sunday
```

**Delta fields:**
| Field | Unit | Formula |
|---|---|---|
| `campaigns.delta` | count | current − previous |
| `recipients.delta` | count | current − previous |
| `unique_opens.delta` | count | current − previous |
| `clicks.delta` | count | current − previous |
| `avg_open_rate.delta` | percentage points (pp) | current − previous |
| `avg_click_rate.delta` | percentage points (pp) | current − previous |
| `*.pct_change` | % | delta ÷ previous × 100 |

---

### 2. Day-Over-Day (Monday vs Monday, etc.)

For each weekday (Mon–Sun), compares campaigns sent on that weekday this week vs the same weekday last week.

```sql
SELECT
    \"Campaign Name\",
    \"Sent Date \"::date                           AS sent_date,
    EXTRACT(ISODOW FROM \"Sent Date \"::date)      AS isodow,  -- 1=Mon, 7=Sun
    \"Recipients\",
    \"UniqueOpened\",
    \"Clicks\",
    \"Unsubscribed\",
    \"Bounced\",
    \"UOpenRate\",
    \"UClickRate\",
    COALESCE(\"URL\", '')                          AS \"URL\",
    \"Subject\"
FROM superage.\"Campaigns\"
WHERE \"Sent Date \" IS NOT NULL
  AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
  AND \"Recipients\" > 95
  AND \"Sent Date \"::date BETWEEN :week_monday AND :week_sunday
ORDER BY \"Sent Date \"::date ASC;
```

Result groups by `isodow` (1–7) for both current and previous week.

---

### 3. Campaign-Level Detail

Each campaign row in `current_campaigns` / `previous_campaigns`:

| Field | Source column | Notes |
|---|---|---|
| `name` | `\"Campaign Name\"` | Full campaign name |
| `subject` | `\"Subject\"` | Email subject line |
| `sent_date` | `\"Sent Date \"` | ISO date (note trailing space in column name) |
| `weekday` | derived from `ISODOW` | \"Monday\" … \"Sunday\" |
| `recipients` | `\"Recipients\"` | Integer |
| `unique_opens` | `\"UniqueOpened\"` | Integer |
| `clicks` | `\"Clicks\"` | Integer |
| `unsubs` | `\"Unsubscribed\"` | Integer |
| `open_rate` | `\"UOpenRate\"` | Float, e.g. `30.21` = 30.21% |
| `click_rate` | `\"UClickRate\"` | Float |
| `url` | `\"URL\"` | Campaign archive/web link |

---

### 4. Weekly Trend (last 12 weeks)

For the sparkline/trend charts: aggregated per ISO week over the last 12 weeks.

```sql
SELECT
    DATE_TRUNC('week', \"Sent Date \"::date)::date   AS week_start,
    COUNT(*)                                        AS campaigns,
    ROUND(AVG(\"UOpenRate\")::numeric, 2)             AS avg_open_rate,
    ROUND(AVG(\"UClickRate\")::numeric, 2)            AS avg_click_rate,
    SUM(\"Recipients\")                               AS recipients,
    SUM(\"Clicks\")                                   AS clicks
FROM superage.\"Campaigns\"
WHERE \"Sent Date \" IS NOT NULL
  AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
  AND \"Recipients\" > 95
  AND \"Sent Date \"::date >= :twelve_weeks_ago
GROUP BY 1
ORDER BY 1 ASC;
```

---

## Output JSON Structure

```json
{
  \"data_as_of\": \"2026-05-13\",
  \"launch_cutoff\": \"2026-05-11\",
  \"current_week\":  { \"start\": \"2026-05-04\", \"end\": \"2026-05-10\" },
  \"previous_week\": { \"start\": \"2026-04-27\", \"end\": \"2026-05-03\" },

  \"week_over_week\": {
    \"current\":  { \"campaigns\": 5, \"recipients\": 4100000, \"avg_open_rate\": 29.5, ... },
    \"previous\": { \"campaigns\": 6, \"recipients\": 4900000, \"avg_open_rate\": 27.1, ... },
    \"delta\": {
      \"campaigns\":      { \"value\": -1, \"pct_change\": -16.7 },
      \"recipients\":     { \"value\": -800000, \"pct_change\": -16.3 },
      \"avg_open_rate\":  { \"value\": 2.4, \"unit\": \"pp\" },
      \"avg_click_rate\": { \"value\": 0.3, \"unit\": \"pp\" }
    }
  },

  \"day_comparison\": [
    {
      \"weekday\": \"Monday\",
      \"current_date\":  \"2026-05-04\",
      \"previous_date\": \"2026-04-27\",
      \"comparison\": { \"current\": {...}, \"previous\": {...}, \"delta\": {...} },
      \"current_campaigns\":  [ { \"name\": \"...\", \"open_rate\": 31.2, ... } ],
      \"previous_campaigns\": [ { \"name\": \"...\", \"open_rate\": 28.7, ... } ]
    },
    ...
  ],

  \"current_campaigns\":  [ { \"name\": \"...\", \"sent_date\": \"2026-05-06\", ... } ],
  \"previous_campaigns\": [ { \"name\": \"...\", \"sent_date\": \"2026-04-29\", ... } ],

  \"weekly_trend\": {
    \"labels\":         [\"Apr 06\", \"Apr 13\", ...],
    \"campaigns\":      [5, 6, ...],
    \"avg_open_rate\":  [28.1, 29.4, ...],
    \"avg_click_rate\": [2.1, 2.3, ...],
    \"recipients\":     [4050000, 4900000, ...],
    \"clicks\":         [85000, 103000, ...]
  }
}
```

---

## Testing Assumptions Against RDS

Run these queries directly on the database to validate lambda output:

```sql
-- 1. Verify campaign filter counts
SELECT COUNT(*) AS qualifying_campaigns
FROM superage.\"Campaigns\"
WHERE \"Sent Date \" IS NOT NULL
  AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
  AND \"Recipients\" > 95;

-- 2. Current week campaigns (replace dates as needed)
SELECT \"Campaign Name\", \"Sent Date \"::date, \"Recipients\", \"UOpenRate\", \"UClickRate\"
FROM superage.\"Campaigns\"
WHERE \"Sent Date \"::date BETWEEN
    DATE_TRUNC('week', (
        SELECT MAX(\"Sent Date \"::date) FROM superage.\"Campaigns\"
        WHERE \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
          AND \"Recipients\" > 95
    ))::date
    AND DATE_TRUNC('week', (
        SELECT MAX(\"Sent Date \"::date) FROM superage.\"Campaigns\"
        WHERE \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
          AND \"Recipients\" > 95
    ))::date + INTERVAL '6 days'
  AND \"Recipients\" > 95
ORDER BY \"Sent Date \"::date;

-- 3. Previous week campaigns
SELECT \"Campaign Name\", \"Sent Date \"::date, \"UOpenRate\", \"UClickRate\"
FROM superage.\"Campaigns\"
WHERE \"Sent Date \"::date BETWEEN
    DATE_TRUNC('week', (
        SELECT MAX(\"Sent Date \"::date) FROM superage.\"Campaigns\"
        WHERE \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
          AND \"Recipients\" > 95
    ))::date - INTERVAL '7 days'
    AND DATE_TRUNC('week', (
        SELECT MAX(\"Sent Date \"::date) FROM superage.\"Campaigns\"
        WHERE \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
          AND \"Recipients\" > 95
    ))::date - INTERVAL '1 day'
  AND \"Recipients\" > 95
ORDER BY \"Sent Date \"::date;

-- 4. Week-over-week avg open rate delta
WITH weeks AS (
    SELECT
        DATE_TRUNC('week', \"Sent Date \"::date)::date AS week_start,
        AVG(\"UOpenRate\") AS avg_open
    FROM superage.\"Campaigns\"
    WHERE \"Sent Date \" IS NOT NULL
      AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
      AND \"Recipients\" > 95
    GROUP BY 1
    ORDER BY 1 DESC
    LIMIT 2
)
SELECT
    MAX(CASE WHEN week_start = (SELECT MAX(week_start) FROM weeks) THEN avg_open END) AS current_avg_open,
    MAX(CASE WHEN week_start = (SELECT MIN(week_start) FROM weeks) THEN avg_open END) AS prev_avg_open,
    MAX(CASE WHEN week_start = (SELECT MAX(week_start) FROM weeks) THEN avg_open END) -
    MAX(CASE WHEN week_start = (SELECT MIN(week_start) FROM weeks) THEN avg_open END) AS delta_pp
FROM weeks;

-- 5. Day-of-week breakdown — current vs previous week
SELECT
    TO_CHAR(\"Sent Date \"::date, 'Day') AS weekday,
    EXTRACT(ISODOW FROM \"Sent Date \"::date) AS isodow,
    \"Sent Date \"::date,
    \"Campaign Name\",
    \"Recipients\",
    ROUND(\"UOpenRate\"::numeric, 2) AS open_rate,
    ROUND(\"UClickRate\"::numeric, 2) AS click_rate
FROM superage.\"Campaigns\"
WHERE \"Sent Date \"::date BETWEEN
    DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '7 days'
    AND DATE_TRUNC('week', CURRENT_DATE)::date + INTERVAL '13 days'
  AND \"Recipients\" > 95
  AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
ORDER BY \"Sent Date \"::date;

-- 6. 12-week trend
SELECT
    DATE_TRUNC('week', \"Sent Date \"::date)::date AS week_start,
    COUNT(*)                                      AS campaigns,
    ROUND(AVG(\"UOpenRate\")::numeric, 2)           AS avg_open_rate,
    ROUND(AVG(\"UClickRate\")::numeric, 2)          AS avg_click_rate,
    SUM(\"Recipients\")                             AS total_recipients,
    SUM(\"Clicks\")                                 AS total_clicks
FROM superage.\"Campaigns\"
WHERE \"Sent Date \" IS NOT NULL
  AND \"Sent Date \"::date <= CURRENT_DATE - INTERVAL '2 days'
  AND \"Recipients\" > 95
  AND \"Sent Date \"::date >= CURRENT_DATE - INTERVAL '84 days'  -- 12 weeks
GROUP BY 1
ORDER BY 1 ASC;

-- 7. Verify URL field exists and has data
SELECT \"Campaign Name\", \"URL\"
FROM superage.\"Campaigns\"
WHERE \"URL\" IS NOT NULL AND \"URL\" != ''
  AND \"Recipients\" > 95
ORDER BY \"Sent Date \" DESC
LIMIT 10;
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DB_SECRET_ARN` | Yes* | — | Secrets Manager ARN for DB credentials |
| `GITHUB_TOKEN` | Yes | — | GitHub fine-grained PAT |
| `GITHUB_REPO` | No | `O-platform/retention-dshb` | Target repo |
| `GITHUB_BRANCH` | No | `main` | Target branch |
| `GITHUB_FILE_PATH` | No | `superage-staging/superage-comparison.json` | Output file path |
| `SA_SCHEMA` | No | `superage` | PostgreSQL schema name |
| `COMMIT_TO_GITHUB` | No | `true` | Set `false` for local testing |

*If `DB_SECRET_ARN` is not set, falls back to `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` env vars.

---

## Deployment Notes

- Deploy as a **separate Lambda function** from `superage_metrics_lambda_updated.py`
- Recommended schedule: **every 6 hours** or after each campaign send (EventBridge rule)
- Output: `superage-staging/superage-comparison.json` (staging) — change `GITHUB_FILE_PATH` for production
- The dashboard reads this file from the same S3/GitHub path as `superage-metrics.json`

---

## Click Analysis Trend Series

The seven queries below were previously in `superage_metrics_lambda_updated.py` and are now produced by this lambda alongside the WoW campaign comparison. The dashboard's **Click Analysis** tab fetches `superage-comparison.json` and reads these keys directly.

Section A uses `superage."Campaigns"` (per-send rollups). Section B uses `superage."Campaigns_Clicks"` (raw click events).

### A.1 — Campaign Clicks Weekly (last 8 ISO weeks)

```sql
WITH weeks AS (
    SELECT generate_series(
        DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '7 weeks',
        DATE_TRUNC('week', CURRENT_DATE)::date,
        INTERVAL '1 week'
    )::date AS week_start
),
agg AS (
    SELECT
        DATE_TRUNC('week', "Sent Date "::date)::date AS week_start,
        COALESCE(SUM("Clicks"), 0)                   AS clicks,
        COALESCE(SUM("UniqueOpened"), 0)             AS unique_opens,
        COUNT(*)                                     AS campaigns
    FROM superage."Campaigns"
    WHERE "Sent Date " IS NOT NULL
      AND "Sent Date "::date < CURRENT_DATE
      AND "Recipients" > 95
      AND "Sent Date "::date >= DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '7 weeks'
    GROUP BY 1
)
SELECT
    w.week_start,
    TO_CHAR(w.week_start, 'Mon DD')                          AS label,
    COALESCE(a.clicks, 0)                                    AS clicks,
    COALESCE(a.unique_opens, 0)                              AS unique_opens,
    COALESCE(a.campaigns, 0)                                 AS campaigns,
    (w.week_start = DATE_TRUNC('week', CURRENT_DATE)::date)  AS is_current
FROM weeks w
LEFT JOIN agg a USING (week_start)
ORDER BY w.week_start;
```

Exposed as `M.campaign_clicks_weekly = { labels, week_starts, clicks, unique_opens, campaigns, is_current }`.

### A.2 — Campaign Clicks Monthly (last 6 months)

```sql
WITH months AS (
    SELECT generate_series(
        DATE_TRUNC('month', CURRENT_DATE)::date - INTERVAL '5 months',
        DATE_TRUNC('month', CURRENT_DATE)::date,
        INTERVAL '1 month'
    )::date AS month_start
),
agg AS (
    SELECT
        DATE_TRUNC('month', "Sent Date "::date)::date AS month_start,
        COALESCE(SUM("Clicks"), 0)                    AS clicks,
        COALESCE(SUM("UniqueOpened"), 0)              AS unique_opens,
        COUNT(*)                                      AS campaigns
    FROM superage."Campaigns"
    WHERE "Sent Date " IS NOT NULL
      AND "Sent Date "::date < CURRENT_DATE
      AND "Recipients" > 95
      AND "Sent Date "::date >= DATE_TRUNC('month', CURRENT_DATE)::date - INTERVAL '5 months'
    GROUP BY 1
)
SELECT
    m.month_start,
    TO_CHAR(m.month_start, 'Mon YYYY')                         AS label,
    COALESCE(a.clicks, 0)                                      AS clicks,
    COALESCE(a.unique_opens, 0)                                AS unique_opens,
    COALESCE(a.campaigns, 0)                                   AS campaigns,
    (m.month_start = DATE_TRUNC('month', CURRENT_DATE)::date)  AS is_current
FROM months m
LEFT JOIN agg a USING (month_start)
ORDER BY m.month_start;
```

Exposed as `M.campaign_clicks_monthly`.

### A.3 — Campaign Clicks Same Weekday (last 5 occurrences)

```sql
WITH d AS (
    SELECT generate_series(
        CURRENT_DATE - INTERVAL '4 weeks',
        CURRENT_DATE,
        INTERVAL '7 days'
    )::date AS day
)
SELECT
    d.day,
    TO_CHAR(d.day, 'Dy Mon DD')                AS label,
    COALESCE(SUM(c."Clicks"), 0)               AS clicks,
    COALESCE(SUM(c."UniqueOpened"), 0)         AS unique_opens,
    COUNT(c.*)                                 AS campaigns,
    (d.day = CURRENT_DATE)                     AS is_current
FROM d
LEFT JOIN superage."Campaigns" c
  ON c."Sent Date "::date = d.day
 AND c."Recipients" > 95
GROUP BY d.day
ORDER BY d.day;
```

Exposed as `M.campaign_clicks_same_weekday`.

### A.4 — Campaign Clicks Same Day-of-Month (last 4 months)

```sql
WITH months AS (
    SELECT generate_series(
        DATE_TRUNC('month', CURRENT_DATE)::date - INTERVAL '3 months',
        DATE_TRUNC('month', CURRENT_DATE)::date,
        INTERVAL '1 month'
    )::date AS month_start
), d AS (
    SELECT LEAST(
        month_start + (EXTRACT(DAY FROM CURRENT_DATE)::int - 1),
        (month_start + INTERVAL '1 month' - INTERVAL '1 day')::date
    ) AS day
    FROM months
)
SELECT
    d.day,
    TO_CHAR(d.day, 'Mon DD, YYYY')             AS label,
    COALESCE(SUM(c."Clicks"), 0)               AS clicks,
    COALESCE(SUM(c."UniqueOpened"), 0)         AS unique_opens,
    COUNT(c.*)                                 AS campaigns,
    (d.day = CURRENT_DATE)                     AS is_current
FROM d
LEFT JOIN superage."Campaigns" c
  ON c."Sent Date "::date = d.day
 AND c."Recipients" > 95
GROUP BY d.day
ORDER BY d.day;
```

Exposed as `M.campaign_clicks_same_dom`.

### B.1 — Raw Clicks Same Weekday (last 5 occurrences of today's weekday)

Kept for backwards compatibility; the dashboard now prefers the
per-weekday payload below (B.1b).

```sql
WITH clicks AS (
    SELECT "Date"::date AS d
    FROM superage."Campaigns_Clicks"
    WHERE "Date" IS NOT NULL
      AND "Date" >= (CURRENT_DATE - INTERVAL '4 weeks')
      AND "Date" <= CURRENT_DATE
),
d AS (
    SELECT generate_series(
        CURRENT_DATE - INTERVAL '4 weeks',
        CURRENT_DATE,
        INTERVAL '7 days'
    )::date AS day
)
SELECT
    d.day,
    TO_CHAR(d.day, 'Dy Mon DD')   AS label,
    COUNT(c.d)                    AS clicks,
    (d.day = CURRENT_DATE)        AS is_current
FROM d
LEFT JOIN clicks c ON c.d = d.day
GROUP BY d.day
ORDER BY d.day;
```

Exposed as `M.raw_clicks_same_weekday`. Each row also carries a
`clicks_no_ss` count — the same query with
`issue_name NOT ILIKE '%sunday spotlight%'` applied — so the Click Analysis
"Include Sunday Spotlight" toggle can switch in/out without a separate query.

### B.1b — Raw Clicks by Weekday (last 5 occurrences of EACH weekday)

Powers the "Same Weekday" grouped-bar chart in the Click Analysis tab.
The dashboard renders 7 weekday groups with N bars (2, 3 or 5) per
group, ordered oldest → most recent. Each bar's tooltip shows the
exact calendar date.

```sql
WITH d AS (
    SELECT day::date AS day
    FROM generate_series(
        CURRENT_DATE - INTERVAL '6 weeks',
        CURRENT_DATE,
        INTERVAL '1 day'
    ) AS day
),
ranked AS (
    SELECT
        d.day,
        TO_CHAR(d.day, 'Dy')                     AS dow,
        ROW_NUMBER() OVER (
            PARTITION BY EXTRACT(DOW FROM d.day)
            ORDER BY d.day DESC
        ) AS rn
    FROM d
),
clicks AS (
    SELECT "Date"::date AS d
    FROM superage."Campaigns_Clicks"
    WHERE "Date" IS NOT NULL
      AND "Date" >= (CURRENT_DATE - INTERVAL '6 weeks')
      AND "Date" <= CURRENT_DATE
)
SELECT
    r.day,
    r.dow,
    TO_CHAR(r.day, 'Dy Mon DD')        AS label,
    COUNT(c.d)                         AS clicks,
    (r.day = CURRENT_DATE)             AS is_current
FROM ranked r
LEFT JOIN clicks c ON c.d = r.day
WHERE r.rn <= 5
GROUP BY r.day, r.dow
ORDER BY r.dow, r.day;
```

Exposed as `M.raw_clicks_by_weekday`, a dict keyed by `Mon`…`Sun`
where each value has `labels[]`, `days[]`, `clicks[]`, `clicks_no_ss[]`,
`is_current[]` in chronological order. `clicks_no_ss[]` is the same count
filtered by `issue_name NOT ILIKE '%sunday spotlight%'` for the SS toggle.

### B.2 — Raw Clicks Weekly (last 12 ISO weeks)

```sql
WITH clicks AS (
    SELECT DATE_TRUNC('week', "Date"::date)::date AS w
    FROM superage."Campaigns_Clicks"
    WHERE "Date" IS NOT NULL
      AND "Date" >= (DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '11 weeks')
      AND "Date" <= CURRENT_DATE
),
weeks AS (
    SELECT generate_series(
        DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '11 weeks',
        DATE_TRUNC('week', CURRENT_DATE)::date,
        INTERVAL '1 week'
    )::date AS week_start
)
SELECT
    w.week_start,
    TO_CHAR(w.week_start, 'Mon DD')                          AS label,
    COUNT(c.w)                                               AS clicks,
    (w.week_start = DATE_TRUNC('week', CURRENT_DATE)::date)  AS is_current
FROM weeks w
LEFT JOIN clicks c ON c.w = w.week_start
GROUP BY w.week_start
ORDER BY w.week_start;
```

Exposed as `M.raw_clicks_weekly`. The dashboard exposes a 4w / 8w /
12w toggle that slices the tail of this series client-side; default
view is 8 weeks. Each entry has `clicks` and `clicks_no_ss` (filtered by
`issue_name NOT ILIKE '%sunday spotlight%'`); the dashboard picks the
appropriate field based on the "Include Sunday Spotlight" toggle.

### B.3 — Raw Clicks Monthly (last 6 months)

```sql
WITH clicks AS (
    SELECT DATE_TRUNC('month', "Date"::date)::date AS m
    FROM superage."Campaigns_Clicks"
    WHERE "Date" IS NOT NULL
      AND "Date" >= (DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '5 months')
      AND "Date" <= CURRENT_DATE
),
months AS (
    SELECT generate_series(
        DATE_TRUNC('month', CURRENT_DATE)::date - INTERVAL '5 months',
        DATE_TRUNC('month', CURRENT_DATE)::date,
        INTERVAL '1 month'
    )::date AS month_start
)
SELECT
    m.month_start,
    TO_CHAR(m.month_start, 'Mon YYYY')                         AS label,
    COUNT(c.m)                                                 AS clicks,
    (m.month_start = DATE_TRUNC('month', CURRENT_DATE)::date)  AS is_current
FROM months m
LEFT JOIN clicks c ON c.m = m.month_start
GROUP BY m.month_start
ORDER BY m.month_start;
```

Exposed as `M.raw_clicks_monthly` with `clicks` and `clicks_no_ss`
(filtered by `issue_name NOT ILIKE '%sunday spotlight%'`).

---

## Section C — Weekly Digest

9 ISO weeks (8 completed + the in-progress current week) of headline
metrics feeding the **Weekly Digest** tab. WoW deltas are computed
client-side so the same source drives both the headline tiles and the
sparklines.

**Recipients threshold:** `>= 200,000` — mass sends only. Segmented /
dedicated campaigns (e.g. 10–20K newsletters, A/B pilots) have atypical
rates that skew a simple `AVG(UOpenRate)` over the week; the 200K cutoff
isolates the main list sends. All other dashboard surfaces keep `> 95`.

```sql
WITH weeks AS (
    SELECT generate_series(
        DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '8 weeks',
        DATE_TRUNC('week', CURRENT_DATE)::date,
        INTERVAL '1 week'
    )::date AS week_start
),
joins AS (
    SELECT DATE_TRUNC('week', date_joined::date)::date AS week_start,
           COUNT(*) AS n
    FROM superage.subscribers
    WHERE date_joined IS NOT NULL
      AND date_joined::date < CURRENT_DATE
      AND date_joined::date >= DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '8 weeks'
    GROUP BY 1
),
unsubs AS (
    SELECT DATE_TRUNC('week', date_unsubscribed::date)::date AS week_start,
           COUNT(*) AS n
    FROM superage.subscribers
    WHERE date_unsubscribed IS NOT NULL
      AND date_unsubscribed::date < CURRENT_DATE
      AND date_unsubscribed::date >= DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '8 weeks'
    GROUP BY 1
),
camps AS (
    SELECT
        DATE_TRUNC('week', "Sent Date "::date)::date AS week_start,
        COUNT(*)                                    AS campaigns_sent,
        COALESCE(SUM("Recipients"), 0)              AS total_sent,
        ROUND(AVG("UOpenRate")::numeric,  2)        AS avg_open_rate,
        ROUND(AVG("UClickRate")::numeric, 2)        AS avg_click_rate
    FROM superage."Campaigns"
    WHERE "Sent Date " IS NOT NULL
      AND "Sent Date "::date < CURRENT_DATE
      AND "Recipients" >= 200000
      AND "Sent Date "::date >= DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '8 weeks'
    GROUP BY 1
),
gh AS (
    SELECT DATE_TRUNC('week', snapshot_date::date)::date AS week_start,
           MAX(total_active) AS active_eow
    FROM superage.growth_history
    WHERE snapshot_date IS NOT NULL
      AND snapshot_date <= CURRENT_DATE
      AND snapshot_date >= DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '8 weeks'
    GROUP BY 1
)
SELECT
    w.week_start,
    TO_CHAR(w.week_start, 'Mon DD')                         AS label,
    COALESCE(j.n, 0)                                        AS new_subs,
    COALESCE(u.n, 0)                                        AS unsubs,
    COALESCE(c.campaigns_sent, 0)                           AS campaigns_sent,
    COALESCE(c.total_sent, 0)                               AS total_sent,
    c.avg_open_rate,
    c.avg_click_rate,
    CASE
        WHEN COALESCE(c.total_sent, 0) = 0 THEN NULL
        ELSE ROUND(COALESCE(u.n, 0)::numeric / c.total_sent * 100, 4)
    END                                                     AS churn_pct_of_sends,
    g.active_eow,
    (w.week_start = DATE_TRUNC('week', CURRENT_DATE)::date) AS is_current
FROM weeks w
LEFT JOIN joins  j ON j.week_start = w.week_start
LEFT JOIN unsubs u ON u.week_start = w.week_start
LEFT JOIN camps  c ON c.week_start = w.week_start
LEFT JOIN gh     g ON g.week_start = w.week_start
ORDER BY w.week_start;
```

Exposed as `M.weekly_digest` with parallel arrays:

```json
{
  "labels":             ["May 04", "May 11", ...],
  "week_starts":        ["2026-05-04", ...],
  "is_current":         [false, ..., true],
  "new_subs":           [...],
  "unsubs":             [...],
  "campaigns_sent":     [...],
  "total_sent":         [...],
  "avg_open_rate":      [...],
  "avg_click_rate":     [...],
  "churn_pct_of_sends": [...],
  "active_eow":         [...],
  "top_sources_by_week": [...],
  "top_editorial_this_week":  [...],
  "top_sponsors_this_week":   [...],
  "top_immersions_this_week": [...]
}
```

`churn_pct_of_sends` = `unsubs / total_sent * 100` (NULL when `total_sent = 0`). `active_eow` may be NULL for the in-progress week if the growth_history snapshot hasn't been written yet.

---

## Section C1 — Top Acquisition Source (last 2 completed ISO weeks)

Counts new subscribers by canonical source label for the **last 2 completed
ISO weeks** (the in-progress current week is excluded). Client picks the
top-subs entry for the most recent completed week to display on the digest tile.

**Label priority:** `sa.acquisition_utm_source → s.source → 'Organic'`.
Canonicalisation rules match `_canon_source()` in the metrics lambda and
`utmLabel()` in `index.html`.

```sql
WITH sa_acq AS (
    SELECT LOWER(TRIM(email)) AS email, acquisition_utm_source
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added', 'resubscribed')
),
src AS (
    SELECT
        DATE_TRUNC('week', s.date_joined::date)::date AS week_start,
        CASE
            WHEN LOWER(COALESCE(
                    NULLIF(TRIM(sa.acquisition_utm_source),''),
                    NULLIF(TRIM(s.source),''), ''))
                 IN ('organic','direct','none','null','(none)','(null)','n/a','-','',
                     'website','homepage','home','web','site')
                THEN 'Organic'
            WHEN ... -- full canonicalisation CASE (same rules as metrics lambda)
            ELSE NULLIF(TRIM(COALESCE(
                    NULLIF(TRIM(sa.acquisition_utm_source),''),
                    NULLIF(TRIM(s.source),''))), '')
        END AS bucket
    FROM superage.subscribers s
    LEFT JOIN sa_acq sa ON sa.email = LOWER(TRIM(s.email))
    WHERE s.date_joined IS NOT NULL
      AND s.date_joined::date >= DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '2 weeks'
      AND s.date_joined::date <  DATE_TRUNC('week', CURRENT_DATE)::date
)
SELECT
    week_start,
    COALESCE(bucket, 'Organic') AS bucket,
    COUNT(*)                    AS subs
FROM src
GROUP BY 1, 2
ORDER BY week_start DESC, subs DESC;
```

Exposed inside `M.weekly_digest.top_sources_by_week` as a list of
`{ week_start, bucket, subs }` objects ordered by week DESC then subs DESC.

---

## Section C2 — Article-Level Activity (last completed ISO week)

Article clicks from `articles_clicks` for the **last completed Mon–Sun**,
restricted to the three placement types the Weekly Digest tab surfaces:
`editorial`, `sponsor`, `immersion`. Bucketed by `issue_date` (no
per-click event date on this table). Up to 200 rows; client slices to top 5
per type.

```sql
WITH wk AS (
    SELECT
        DATE_TRUNC('week', CURRENT_DATE)::date - INTERVAL '1 week' AS wk_start,
        DATE_TRUNC('week', CURRENT_DATE)::date                      AS wk_end_excl
)
SELECT
    LOWER(TRIM(ac.type))   AS atype,
    ac.article_title,
    ac.url,
    ac.issue_name,
    ac.issue_date::date    AS issue_date,
    SUM(ac.unique_clicks)  AS unique_clicks,
    SUM(ac.non_unique_clicks) AS total_clicks
FROM superage.articles_clicks ac, wk
WHERE ac.issue_date IS NOT NULL
  AND ac.issue_date::date >= wk.wk_start
  AND ac.issue_date::date <  wk.wk_end_excl
  AND LOWER(TRIM(ac.type)) IN ('editorial','sponsor','immersion')
GROUP BY 1, ac.article_title, ac.url, ac.issue_name, ac.issue_date::date
ORDER BY unique_clicks DESC NULLS LAST
LIMIT 200;
```

Exposed inside `M.weekly_digest` as three arrays:

| Key | Filter |
|---|---|
| `top_editorial_this_week` | `atype = 'editorial'` |
| `top_sponsors_this_week` | `atype = 'sponsor'` |
| `top_immersions_this_week` | `atype = 'immersion'` |

Each entry: `{ title, url, issue_name, issue_date, unique_clicks, total_clicks }`.

---

## Dashboard Wiring

The dashboard loads both JSON files in parallel and merges:

```js
Promise.all([
  fetch('./superage-metrics.json').then(r => r.json()),
  fetch('./superage-comparison.json').then(r => r.ok ? r.json() : {}).catch(() => ({}))
]).then(([metrics, comparison]) => {
  const M = Object.assign({}, metrics, comparison);
  // …renderers consume M
});
```

The comparison fetch tolerates 404 — if the comparison lambda hasn't run yet, the dashboard still works (Click Analysis charts will simply show empty).
