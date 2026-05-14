# SuperAge Analytics Dashboard — Metrics Reference

This document describes every metric, chart, and data source used in the SuperAge dashboard,
including the exact SQL queries run by the Lambda so anomalies can be reproduced and debugged directly in RDS.

> **Maintenance contract**
> Every SQL query run by `superage_metrics_lambda_updated.py` and `superage_comparison_lambda.py` must be
> mirrored in this file (or in `COMPARISON_METRICS.md` for the comparison lambda). Any change to a lambda
> query — added/removed columns, new filter, new aggregate, new table — must be reflected here in the same
> change, with the SQL block kept literally copy-pasteable so it can be re-run against RDS for validation.

> **Note on schema placeholder:** In every query below, replace `superage` with the value of the `SA_SCHEMA`
> env var if it differs in your environment (default: `superage`).

---

## How the Dashboard Works

The Lambda function (`superage_metrics_lambda_updated.py`) runs on a schedule, queries the
SuperAge database, and commits `superage-metrics.json` to `superage-staging/superage-metrics.json`
on GitHub. The HTML dashboard fetches this JSON and renders it client-side.

---

## Global Date Rule

For every table that has a reliable date column, the dashboard **excludes today and future dates**:

```
<date_column>::date < CURRENT_DATE
```

| Section | Date field applied |
|---|---|
| Subscribers | `date_joined`, `date_unsubscribed` |
| Campaigns | `"Sent Date "` (note: trailing space in column name) |
| Article placements | `created_at` |
| WordPress articles | `published_date` |
| Revenue/sales | Not filtered — grouped by `issue_date`; no reliable sent/closed date available |
| Quiz responses | Not filtered — no submission date column confirmed |
| Subscriber clicks | Not filtered — no click date column confirmed |

---

## Campaign Filter

All campaign queries apply two filters:

- `"Sent Date "::date < CURRENT_DATE` — excludes unsent / future campaigns
- `"Recipients" > 1000` — excludes test sends and low-volume internal mailings

In the lambda this is stored as:
```sql
"Sent Date " IS NOT NULL
AND "Sent Date "::date < CURRENT_DATE
AND "Recipients" > 1000
```

---

## 1. Overview

The overview tab surfaces the most important headline numbers across all sections.

| Metric | What it measures |
|---|---|
| Total Subscribers | All-time subscriber count |
| Active Subscribers | Subscribers currently in Active state |
| Active Rate | Active ÷ Total |
| Avg Open Rate | Average unique open rate across campaigns (≥ 1,000 recipients) |
| Avg Click Rate | Average click rate across campaigns |
| Best Open Rate | Highest open rate recorded by a single campaign |
| Campaigns Sent | Count of campaigns with ≥ 1,000 recipients sent before today |
| Quiz Takers | Subscribers who completed the longevity quiz |
| High Engagement (60d) | Subscribers flagged as high-engagement in the past 60 days |
| Total Unique Opens | Sum of unique opens across all qualifying campaigns |
| Campaign Clicks | Sum of all campaign clicks |
| Article Clickers | Subscribers who clicked at least one article |

**Charts:**
- **Subscriber Status Mix** — Donut chart of subscriber states (Active, Unsubscribed, Bounced, Deleted).
- **Campaign Performance Trend** — Line chart of open rate and click rate across the last 30 campaigns, ordered by send date.
- **Subscriber Growth Over Time** — Green bars = gained (new subscribers), red bars = lost (unsubscribes), blue line = total active subscribers at month end (right Y axis). 18-month sliding window.

---

## 2. Campaigns

All campaign metrics use the filter: `Recipients > 1000 AND Sent Date < today`.

### KPIs

KPIs are surfaced by the lambda as the aggregate values below (Q8) and are **also recomputed client-side** from `campaign_table` whenever the dashboard's Sunday Spotlight toggle changes scope. Both paths share the same formulas.

| Metric | Calculation |
|---|---|
| Total Campaigns | `COUNT(*)` of qualifying campaigns |
| Total Recipients | `SUM("Recipients")` |
| Total Unique Opens | `SUM("UniqueOpened")` |
| Total Clicks | `SUM("Clicks")` |
| Total Unsubscribes | `SUM("Unsubscribed")` |
| Avg Open Rate | `AVG("UOpenRate")` |
| Avg Click Rate | `AVG("UClickRate")` |
| Best Open Rate | `MAX("UOpenRate")` |
| Best Click Rate | `MAX("UClickRate")` |

### Charts

- **Top 15 Campaigns by Open Rate** — Horizontal bar, ranked by `UOpenRate`. Lambda emits `top_campaigns` (Q10); the dashboard re-ranks the in-scope set when Sunday Spotlight is toggled off.
- **Open Rate vs Click Rate** — Scatter plot; each point is one campaign in the current scope.

### Campaign Table

Client-paginated table (20 rows / page) of all qualifying campaigns sorted by send date (most recent first). Driven by `campaign_table` (Q9).

### Sunday Spotlight Toggle (UI-side filter)

The toggle excludes campaigns whose `name` contains `sunday spotlight` (case-insensitive) before KPIs, charts and the paginated table are rendered. It is purely client-side — no SQL change.

---

## 3. Website Content

This section measures how SuperAge's website articles perform when featured in campaigns.

### Content Summary KPIs

| Metric | What it measures |
|---|---|
| Article Placements | Total rows in the article placements table |
| Unique Clicks | One click counted per subscriber per article |
| Total Clicks | All clicks including repeat visits |
| Active Clickers | Subscribers who clicked at least one article |

> **Excluded:** content types `games` and `waitlist` are excluded from type/category/tag breakdowns.

---

## 4. Audience

### Subscriber KPIs

Total subscribers, active, unsubscribed, bounced, and high-engagement (60-day window).

### Acquisition by UTM Source

Grouped by `utm_source` only. Engagement quality table shows subscribers, clickers, unique clicks, total clicks, avg clicks/sub, and clicker rate per source.

### UTM Source Subscriber Click Activity

Joins `subscriber_clicks` to `subscribers` to show which acquisition source drives the most article click activity.

### Click Distribution

How many articles each subscriber has clicked: 1 / 2–5 / 6–10 / 11–20 / 20+.

---

## 5. Longevity Quiz

| Metric | What it measures |
|---|---|
| Quiz Takers | Subscribers with a completed longevity quiz |
| Avg Longevity Score | Average score out of 100 |
| Avg Age | Average age of quiz takers |
| Fitness Quiz | Subscribers who completed the fitness assessment |
| Menu Quiz | Subscribers who completed the nutrition quiz |

Demographics: age distribution, gender, marital status, longevity score buckets, exercise frequency, sleep hours, education level, body weight profile.

---

## 6. Revenue & Sponsors

Source: `superage.sa_airtable_sales`.

| Metric | What it measures |
|---|---|
| Total Revenue | Sum of all closed deal amounts |
| Avg Deal Size | Average revenue per placement |
| Avg ECPM | Effective cost per thousand impressions |

---

## 7. Subscriber Retention

Tracks L1 and L2 subscriber tiers. KPIs: total, active, churned, avg lifespan, churn rate.

Charts: Survival Curve (% still active at day 30/60/90/180/365), Days Active Distribution, Monthly Churn Volume.

---

## 8. Cohort Analysis

Groups subscribers by join month and tracks % still active at M+1 through M+12.

| KPI | Source |
|---|---|
| Total Cohorts | Distinct join-month groups |
| Avg 90-Day Retention | Average M+3 retention across all cohorts with ≥ 20 subscribers |
| Best / Worst Cohort | Highest / lowest M+3 retention rate |

Charts: Retention Heatmap, 90-Day Churn Rate by Acquisition Source, 90-Day Retention by UTM Source.

Cohort Performance Table columns: Cohort, Size, Still Active, Retention Rate, Churn Rate, Early Churn (90d), Campaigns That Month.

---

---

# SQL Query Reference

All queries use `superage` as the schema. Replace with your `SA_SCHEMA` value as needed.

---

## Q1 — Overview / Audience: Subscriber Overview KPIs

**Definitions** (per product):
- **Total Subscribers** = `COUNT(*) WHERE state = 'Active'`. This is the active base, not the row count across all states.
- **Total (all states)** = raw `COUNT(*)` — exposed as `total_all_states` and used as the denominator for state-mix ratios (unsub %, bounced %, deleted %).
- **Active (Send-to)** = subscribers whose `engagement_segment` is NOT in (`Ghost`, `Zombie`, `Dormant`) and is not NULL. See Q1b.

```sql
SELECT
    COUNT(*) AS total_all_states,
    COUNT(*) FILTER (WHERE state = 'Active')       AS active,
    COUNT(*) FILTER (WHERE state = 'Unsubscribed') AS unsubscribed,
    COUNT(*) FILTER (WHERE state = 'Bounced')      AS bounced,
    COUNT(*) FILTER (WHERE state = 'Deleted')      AS deleted,
    COUNT(*) FILTER (WHERE has_taken_longevity_quiz = true) AS quiz_takers,
    COUNT(*) FILTER (WHERE took_fitness_quiz::text = '1')   AS fitness_quiz_takers,
    COUNT(*) FILTER (WHERE took_menu_quiz::text = '1')      AS menu_quiz_takers,
    COUNT(*) FILTER (
        WHERE high_engagement_60d IS NOT NULL AND high_engagement_60d::text != ''
    ) AS high_eng
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE;
```

The lambda assigns `total_subscribers = active` from this row.

## Q1b — Audience: Active (Send-to) Count

Anyone whose `engagement_segment` is NOT one of the inactive buckets.

```sql
SELECT
    COUNT(*) FILTER (
        WHERE engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
    ) AS send_to_active
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE;
```

Exposed as `M.send_to_active`; `send_to_rate = send_to_active / total_subscribers` (active base).

## Q2 — Overview: Subscriber Status Mix (Donut Chart)

```sql
SELECT COALESCE(NULLIF(state, ''), 'Unknown') AS state, COUNT(*) AS cnt
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE
GROUP BY 1 ORDER BY 2 DESC;
```

## Q3 — Overview: Subscriber Growth Over Time (growth_history table)

Single query replaces the old Q3 / Q4 / Q4b subscriber + generate_series approach.

```sql
SELECT
    TO_CHAR(DATE_TRUNC('month', snapshot_date), 'YYYY-MM') AS month_label,
    SUM(gained)       AS gained,
    SUM(lost)         AS lost,
    MAX(total_active) AS total_active
FROM superage.growth_history
WHERE snapshot_date < CURRENT_DATE
GROUP BY DATE_TRUNC('month', snapshot_date)
ORDER BY 1;
```

JSON exposed as `M.subscriber_monthly` with keys:
- `labels` — `YYYY-MM` strings
- `new_subs` ← `gained` (green bars, left Y axis)
- `unsubs` ← `lost` (red bars, left Y axis)
- `active_count` ← `total_active` (purple line, right Y axis — the "Total Subscribers (Active)" line)

The dashboard derives `net change` client-side as `new_subs[i] - unsubs[i]` and renders it as a blue line on the left axis. `MAX(total_active)` captures the end-of-month snapshot when the table has daily rows.

## Q5 — Audience: Subscription Level Distribution

```sql
SELECT COALESCE(NULLIF(sub_level, ''), 'Unknown') AS level, COUNT(*) AS cnt
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE
GROUP BY 1 ORDER BY 2 DESC;
```

## Q6 — Audience: Top Acquisition Sources by Volume

```sql
SELECT COALESCE(NULLIF(sub_source, ''), 'Direct/Unknown') AS source, COUNT(*) AS cnt
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

## Q7 — Audience: US-Based Subscriber Count

```sql
SELECT COUNT(*) AS n
FROM superage.subscribers
WHERE us_based_notification = 'Yes'
  AND date_joined::date < CURRENT_DATE;
```

## Q8 — Campaigns: Campaign Aggregate KPIs

```sql
SELECT
    COUNT(*) AS n,
    COALESCE(SUM("Recipients"), 0)       AS total_recipients,
    COALESCE(SUM("UniqueOpened"), 0)     AS total_unique_opens,
    COALESCE(SUM("Clicks"), 0)           AS total_clicks,
    COALESCE(SUM("Unsubscribed"), 0)     AS total_unsubs,
    COALESCE(SUM("Bounced"), 0)          AS total_bounced,
    COALESCE(SUM("SpamComplaints"), 0)   AS total_spam,
    ROUND(AVG("UOpenRate")::numeric,  2) AS avg_open_rate,
    ROUND(AVG("UClickRate")::numeric, 2) AS avg_click_rate,
    MAX("UOpenRate")  AS best_open_rate,
    MAX("UClickRate") AS best_click_rate
FROM superage."Campaigns"
WHERE "Sent Date " IS NOT NULL
  AND "Sent Date "::date < CURRENT_DATE
  AND "Recipients" > 1000;
```

## Q9 — Campaigns: All Campaigns Table + Open Rate Trend + Scatter Chart

```sql
SELECT
    "Campaign Name", "Sent Date ", "Subject",
    "Recipients", "TotalOpened", "UniqueOpened",
    "Clicks", "Unsubscribed", "Bounced", "SpamComplaints",
    "UOpenRate", "UClickRate",
    COALESCE("URL", '') AS "URL"
FROM superage."Campaigns"
WHERE "Sent Date " IS NOT NULL
  AND "Sent Date "::date < CURRENT_DATE
  AND "Recipients" > 1000
ORDER BY "Sent Date " ASC;
```

Feeds `campaign_table` (dashboard list, scatter chart) and `campaign_trend` (line chart).
`URL` becomes the click-through link on the campaign name in the dashboard table.

## Q10 — Campaigns: Top 15 Campaigns by Open Rate (Bar Chart) + Overview "Top Campaign Open Rates" table

```sql
SELECT
    "Campaign Name", "Sent Date ", "Recipients", "UniqueOpened",
    "Clicks", "Unsubscribed", "UOpenRate", "UClickRate",
    COALESCE("URL", '') AS "URL"
FROM superage."Campaigns"
WHERE "Sent Date " IS NOT NULL
  AND "Sent Date "::date < CURRENT_DATE
  AND "Recipients" > 1000
ORDER BY "UOpenRate" DESC NULLS LAST
LIMIT 15;
```

Feeds `M.top_campaigns[]` which is consumed by both the Campaigns tab "Top 15 by Open Rate" bar chart and the Overview "Top Campaign Open Rates" table. `recipients` is emitted as a raw integer (not a pre-formatted string). The `url` field becomes the click-through link on the campaign name in the Overview table.

## Q11 — Website Content: Article Placements KPIs

```sql
SELECT
    COUNT(*) AS placements,
    COALESCE(SUM(unique_clicks), 0)     AS unique_clicks,
    COALESCE(SUM(non_unique_clicks), 0) AS non_unique_clicks
FROM superage.articles_clicks ac;
```

## Q12 — Website Content: Content Type Performance Table

```sql
SELECT
    COALESCE(NULLIF(type, ''), 'unknown') AS content_type,
    COUNT(*) AS placements,
    COALESCE(SUM(unique_clicks), 0) AS unique_clicks,
    COALESCE(SUM(non_unique_clicks), 0) AS non_unique_clicks,
    ROUND(COALESCE(SUM(unique_clicks), 0)::numeric / NULLIF(COUNT(*), 0), 1) AS avg_unique_clicks
FROM superage.articles_clicks ac
WHERE LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')
GROUP BY 1
ORDER BY unique_clicks DESC NULLS LAST;
```

## Q13 — Website Content: Top Articles by Reader Engagement (Table)

```sql
SELECT
    article_title, issue_name, url, type,
    story_position, position_category,
    unique_clicks, non_unique_clicks
FROM superage.articles_clicks ac
ORDER BY unique_clicks DESC NULLS LAST
LIMIT 40;
```

## Q14 — Website Content: Articles Published per Category (Bar Chart)

```sql
WITH wa AS (
    SELECT
        article_url,
        categories,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.wordpress_articles
    WHERE (published_date IS NULL OR published_date::date < CURRENT_DATE)
), cats AS (
    SELECT
        NULLIF(TRIM(cat), '') AS label,
        COUNT(DISTINCT article_url) AS article_count
    FROM wa
    CROSS JOIN LATERAL regexp_split_to_table(
        COALESCE(NULLIF(categories, ''), 'Uncategorized'), '\s*,\s*'
    ) AS cat
    WHERE NULLIF(TRIM(cat), '') IS NOT NULL
    GROUP BY 1
)
SELECT label, article_count FROM cats
ORDER BY article_count DESC, label
LIMIT 15;
```

## Q15 — Website Content: Unique Clicks by Category (Bar Chart)

```sql
WITH ac AS (
    SELECT
        article_title, url, unique_clicks, non_unique_clicks,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.articles_clicks
    WHERE LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')
), wa AS (
    SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                                       '^https?://(www[.])?', ''))
        article_url, categories,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.wordpress_articles
    WHERE (published_date IS NULL OR published_date::date < CURRENT_DATE)
    ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                             '^https?://(www[.])?', ''),
             modified_date DESC NULLS LAST
), joined AS (
    SELECT
        COALESCE(NULLIF(ac.norm_url, ''), ac.article_title) AS article_key,
        ac.unique_clicks, ac.non_unique_clicks, wa.categories
    FROM ac LEFT JOIN wa ON ac.norm_url = wa.norm_url
), cats AS (
    SELECT
        NULLIF(TRIM(cat), '') AS label,
        COUNT(DISTINCT article_key)             AS article_count,
        COALESCE(SUM(unique_clicks), 0)         AS unique_clicks,
        COALESCE(SUM(non_unique_clicks), 0)     AS non_unique_clicks,
        ROUND(COALESCE(SUM(unique_clicks), 0)::numeric
              / NULLIF(COUNT(DISTINCT article_key), 0), 1) AS avg_unique_clicks
    FROM joined
    CROSS JOIN LATERAL regexp_split_to_table(
        COALESCE(NULLIF(categories, ''), 'Uncategorized'), '\s*,\s*'
    ) AS cat
    WHERE NULLIF(TRIM(cat), '') IS NOT NULL
    GROUP BY 1
)
SELECT label, article_count, unique_clicks, non_unique_clicks, avg_unique_clicks
FROM cats
ORDER BY unique_clicks DESC, label
LIMIT 15;
```

## Q16 — Website Content: Top Tags by Unique Clicks + Tag Performance Table

```sql
WITH ac AS (
    SELECT
        article_title, url, unique_clicks, non_unique_clicks,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.articles_clicks
    WHERE LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')
), wa AS (
    SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                                       '^https?://(www[.])?', ''))
        article_url, tags,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.wordpress_articles
    WHERE (published_date IS NULL OR published_date::date < CURRENT_DATE)
    ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                             '^https?://(www[.])?', ''),
             modified_date DESC NULLS LAST
), joined AS (
    SELECT
        COALESCE(NULLIF(ac.norm_url, ''), ac.article_title) AS article_key,
        ac.unique_clicks, ac.non_unique_clicks, wa.tags
    FROM ac LEFT JOIN wa ON ac.norm_url = wa.norm_url
), tag_rows AS (
    SELECT
        NULLIF(TRIM(tag), '') AS label,
        COUNT(DISTINCT article_key)             AS article_count,
        COALESCE(SUM(unique_clicks), 0)         AS unique_clicks,
        COALESCE(SUM(non_unique_clicks), 0)     AS non_unique_clicks,
        ROUND(COALESCE(SUM(unique_clicks), 0)::numeric
              / NULLIF(COUNT(DISTINCT article_key), 0), 1) AS avg_unique_clicks
    FROM joined
    CROSS JOIN LATERAL regexp_split_to_table(
        COALESCE(NULLIF(tags, ''), 'Untagged'), '\s*,\s*'
    ) AS tag
    WHERE NULLIF(TRIM(tag), '') IS NOT NULL
    GROUP BY 1
)
SELECT label, article_count, unique_clicks, non_unique_clicks, avg_unique_clicks
FROM tag_rows
ORDER BY unique_clicks DESC, label
LIMIT 20;
```

## Q16c–Q16i — Click Analysis trend queries (MOVED)

The four campaign-aggregate trend queries and the three raw-click-event
trend queries that power the **Click Analysis** tab are produced by the
**comparison lambda** (`superage_comparison_lambda.py`), not this lambda.
See [`COMPARISON_METRICS.md`](./COMPARISON_METRICS.md) for the SQL and JSON
shape. The dashboard fetches `superage-comparison.json` alongside
`superage-metrics.json` and merges them client-side.

## Q16b — Content Reference: Article Drill-Down Table (content_drill_table)

Source of the **Content Reference** tab. One row per `articles_clicks` placement, **inner-joined** to the most-recently-modified `wordpress_articles` row that shares the same normalized URL. The inner join is intentional: `articles_clicks` also records sponsor placements that are not WordPress articles — those are out of scope for this view. Returns up to 300 rows.

`categories` and `tags` are returned as the raw **comma-separated strings** stored in WordPress (e.g. `"longevity, health"`). The dashboard splits them on comma at render time to build per-value filter dropdowns and match individual categories/tags. `written_by` is single-valued — used as-is. `position_category` (`high` / `medium` / `low`) is sourced directly from `articles_clicks` and drives the dashboard's position-category filter, KPI chart, and "Sleeper Hits" insight.

```sql
WITH ac AS (
    SELECT
        article_title, url, unique_clicks, non_unique_clicks,
        story_position, position_category,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.articles_clicks
    WHERE LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')
), wa AS (
    SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                                       '^https?://(www[.])?', ''))
        article_url,
        COALESCE(NULLIF(TRIM(categories), ''), 'Uncategorized') AS categories,
        COALESCE(NULLIF(TRIM(tags), ''), '')                    AS tags,
        COALESCE(NULLIF(TRIM(written_by), ''), 'Unknown')       AS written_by,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.wordpress_articles
    WHERE (published_date IS NULL OR published_date::date < CURRENT_DATE)
    ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                             '^https?://(www[.])?', ''),
             modified_date DESC NULLS LAST
)
SELECT
    ac.article_title  AS title,
    ac.url,
    ac.unique_clicks,
    ac.non_unique_clicks,
    ac.story_position,
    ac.position_category,
    wa.categories,
    wa.tags,
    wa.written_by
FROM ac
INNER JOIN wa ON ac.norm_url = wa.norm_url
ORDER BY ac.unique_clicks DESC NULLS LAST
LIMIT 300;
```

**JSON output** (`M.content_drill_table[]`): `title`, `url`, `unique_clicks`, `total_clicks` (renamed from `non_unique_clicks`), `story_position`, `position_category`, `categories`, `tags`, `written_by`.

**Dashboard filters** (client-side, all scope the whole tab — KPIs, position-category chart, sleeper-hits insight, table+pagination):
- Position Cat: exact match on `position_category` (`high` / `medium` / `low`).
- Position: exact match on `story_position`; `No position` selects rows where `story_position` is null/0.
- Author: exact match on `written_by`.
- Category: row matches if `categories.split(',').map(trim)` contains the selected value.
- Tag: row matches if `tags.split(',').map(trim)` contains the selected value.
- Title search: case-insensitive substring match.

**Sleeper Hits** insight: top 10 rows with `position_category = 'low'` ordered by `unique_clicks DESC` — surfaces articles placed near the bottom of an issue that nonetheless drew strong engagement.

---

## Q17 — ~~Hidden Gems~~ *(Section removed from dashboard)*

```sql
WITH ac AS (
    SELECT
        article_title, issue_name, url, type,
        story_position, position_category,
        unique_clicks, non_unique_clicks,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.articles_clicks
    WHERE LOWER(COALESCE(position_category, '')) IN ('low', 'medium')
), wa AS (
    SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                                       '^https?://(www[.])?', ''))
        article_url, categories, tags,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.wordpress_articles
    WHERE (published_date IS NULL OR published_date::date < CURRENT_DATE)
    ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                             '^https?://(www[.])?', ''),
             modified_date DESC NULLS LAST
)
SELECT
    ac.article_title, ac.issue_name, ac.type,
    ac.story_position, ac.position_category,
    ac.unique_clicks, ac.non_unique_clicks,
    wa.categories, wa.tags
FROM ac LEFT JOIN wa ON ac.norm_url = wa.norm_url
ORDER BY ac.unique_clicks DESC NULLS LAST
LIMIT 25;
```

## Q18 — Audience: Subscriber Click Distribution (1 / 2–5 / 6–10 / 11–20 / 20+)

```sql
SELECT
    COUNT(*) FILTER (WHERE unique_clicks = 1)               AS c1,
    COUNT(*) FILTER (WHERE unique_clicks BETWEEN 2 AND 5)   AS c2_5,
    COUNT(*) FILTER (WHERE unique_clicks BETWEEN 6 AND 10)  AS c6_10,
    COUNT(*) FILTER (WHERE unique_clicks BETWEEN 11 AND 20) AS c11_20,
    COUNT(*) FILTER (WHERE unique_clicks > 20)              AS c20plus,
    COUNT(*) AS total_clickers
FROM superage.subscriber_clicks;
```

## Q19 — Audience: Acquisition Quality by UTM Source (Engagement Table)

```sql
WITH s AS (
    SELECT
        LOWER(TRIM(email)) AS email,
        COALESCE(NULLIF(TRIM(utm_source), ''), NULLIF(TRIM(source), ''), 'Organic') AS label
    FROM superage.subscribers
    WHERE email IS NOT NULL AND TRIM(email) != ''
      AND date_joined::date < CURRENT_DATE
), sc AS (
    SELECT
        LOWER(TRIM(email_address)) AS email,
        SUM(unique_clicks)     AS unique_clicks,
        SUM(non_unique_clicks) AS non_unique_clicks
    FROM superage.subscriber_clicks
    WHERE email_address IS NOT NULL AND TRIM(email_address) != ''
    GROUP BY 1
)
SELECT
    s.label,
    COUNT(*) AS subscribers,
    COUNT(sc.email) AS clickers,
    COALESCE(SUM(sc.unique_clicks), 0)     AS unique_clicks,
    COALESCE(SUM(sc.non_unique_clicks), 0) AS non_unique_clicks,
    ROUND(COALESCE(SUM(sc.unique_clicks), 0)::numeric
          / NULLIF(COUNT(*), 0), 2) AS avg_unique_clicks_per_subscriber,
    ROUND(COUNT(sc.email)::numeric / NULLIF(COUNT(*), 0) * 100, 1) AS clicker_rate
FROM s LEFT JOIN sc ON s.email = sc.email
GROUP BY 1
ORDER BY unique_clicks DESC NULLS LAST, subscribers DESC
LIMIT 12;
```

## Q20 — Audience: UTM Source Subscriber Click Activity

```sql
SELECT
    COALESCE(NULLIF(TRIM(s.utm_source), ''), NULLIF(TRIM(s.source), ''), 'Organic') AS label,
    COUNT(DISTINCT sc.email_address) AS clickers,
    COALESCE(SUM(sc.unique_clicks), 0)     AS unique_clicks,
    COALESCE(SUM(sc.non_unique_clicks), 0) AS total_clicks,
    ROUND(COALESCE(SUM(sc.unique_clicks), 0)::numeric
          / NULLIF(COUNT(DISTINCT sc.email_address), 0), 1) AS avg_per_clicker
FROM superage.subscriber_clicks sc
JOIN superage.subscribers s
  ON LOWER(TRIM(s.email)) = LOWER(TRIM(sc.email_address))
GROUP BY 1
ORDER BY unique_clicks DESC NULLS LAST
LIMIT 12;
```

## Q21 — Longevity Quiz: Quiz KPIs

```sql
SELECT
    COUNT(*) AS n,
    ROUND(AVG(longevity_score)::numeric, 1) AS avg_score,
    ROUND(MIN(longevity_score)::numeric, 1) AS min_score,
    ROUND(MAX(longevity_score)::numeric, 1) AS max_score,
    ROUND(AVG(age)::numeric, 1) AS avg_age
FROM superage.subscriber_quiz
WHERE longevity_score IS NOT NULL;
```

## Q22 — Longevity Quiz: Age Distribution

```sql
SELECT
    CASE
        WHEN age < 35 THEN 'Under 35'
        WHEN age < 45 THEN '35–44'
        WHEN age < 55 THEN '45–54'
        WHEN age < 65 THEN '55–64'
        WHEN age < 75 THEN '65–74'
        ELSE '75+'
    END AS bucket,
    COUNT(*) AS cnt
FROM superage.subscriber_quiz
WHERE age IS NOT NULL
GROUP BY 1 ORDER BY MIN(age);
```

## Q23 — Longevity Quiz: Gender Distribution

```sql
SELECT COALESCE(NULLIF(gender, ''), 'Unknown') AS gender, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC;
```

## Q24 — Longevity Quiz: Longevity Score Buckets

```sql
SELECT
    COUNT(*) FILTER (WHERE longevity_score < 60)                    AS s_under60,
    COUNT(*) FILTER (WHERE longevity_score BETWEEN 60 AND 69.99)    AS s_60_70,
    COUNT(*) FILTER (WHERE longevity_score BETWEEN 70 AND 79.99)    AS s_70_80,
    COUNT(*) FILTER (WHERE longevity_score BETWEEN 80 AND 89.99)    AS s_80_90,
    COUNT(*) FILTER (WHERE longevity_score >= 90)                   AS s_90plus
FROM superage.subscriber_quiz
WHERE longevity_score IS NOT NULL;
```

## Q25 — Longevity Quiz: Exercise Frequency

```sql
SELECT
    COALESCE(
        NULLIF(exercise_freq, ''),
        NULLIF(exercise_freq_male, ''),
        NULLIF(exercise_freq_female, ''),
        'Unknown'
    ) AS freq,
    COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC LIMIT 8;
```

## Q26 — Longevity Quiz: Sleep Hours

```sql
SELECT COALESCE(NULLIF(sleep_hours, ''), 'Unknown') AS sleep, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC;
```

## Q27 — Longevity Quiz: Education Level

```sql
SELECT COALESCE(NULLIF(education_level, ''), 'Unknown') AS label, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC LIMIT 12;
```

## Q28 — Longevity Quiz: Marital Status

```sql
SELECT COALESCE(NULLIF(marital_status, ''), 'Unknown') AS label, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC LIMIT 12;
```

## Q29 — Longevity Quiz: Body Weight Profile

```sql
SELECT
    CASE
        WHEN is_obese IS NULL THEN 'Unknown'
        WHEN LOWER(is_obese::text) IN ('1','true','yes','y') THEN 'Obese'
        WHEN LOWER(is_obese::text) IN ('0','false','no','n') THEN 'Not Obese'
        ELSE is_obese::text
    END AS label,
    COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC;
```

## Q30 — Revenue & Sponsors: Revenue KPIs

```sql
SELECT
    COUNT(*) AS n,
    SUM(NULLIF("$_line_amount", '')::numeric)  AS total_revenue,
    AVG(NULLIF("$_line_amount", '')::numeric)  AS avg_deal,
    MAX(NULLIF("$_line_amount", '')::numeric)  AS max_deal
FROM superage.sa_airtable_sales
WHERE "$_line_amount" IS NOT NULL AND "$_line_amount" != '';
```

## Q31 — Revenue & Sponsors: Monthly Revenue & Deal Volume (Bar+Line Chart)

```sql
SELECT
    DATE_TRUNC('month', issue_date::date)::date         AS month_start,
    TO_CHAR(DATE_TRUNC('month', issue_date::date), 'Mon YYYY') AS month_label,
    SUM(NULLIF("$_line_amount", '')::numeric)           AS revenue,
    COUNT(*)                                            AS deals
FROM superage.sa_airtable_sales
WHERE issue_date IS NOT NULL
  AND TRIM(CAST(issue_date AS TEXT)) != ''
  AND "$_line_amount" IS NOT NULL AND "$_line_amount" != ''
GROUP BY 1, 2
ORDER BY 1;
```

**Future-month treatment (dashboard-side):** months whose `month_label` parses to a date strictly later than the current calendar month are rendered with reduced-opacity bars (revenue) and a dashed line segment (deals). Past + current months render solid. The split is computed client-side from the same array — the lambda emits the full series without partitioning.

## Q32 — Revenue & Sponsors: Top Sponsors by Revenue (Table)

```sql
SELECT
    COALESCE("sponsor_name"->>0, "sponsor_name"::text, 'Unknown') AS sponsor,
    COUNT(*) AS deals,
    SUM(NULLIF("$_line_amount", '')::numeric)  AS revenue,
    AVG(NULLIF(ecpm, '')::numeric)             AS avg_ecpm
FROM superage.sa_airtable_sales
WHERE "$_line_amount" IS NOT NULL AND "$_line_amount" != ''
GROUP BY 1 ORDER BY 3 DESC NULLS LAST LIMIT 10;
```

## Q33 — Revenue & Sponsors: Sponsor Type Distribution (Donut Chart)

```sql
SELECT COALESCE(NULLIF(sponsor_type, ''), 'Unknown') AS stype, COUNT(*) AS cnt
FROM superage.sa_airtable_sales
GROUP BY 1 ORDER BY 2 DESC;
```

## Q34 — Revenue & Sponsors: Avg ECPM (KPI)

```sql
SELECT
    ROUND(AVG(NULLIF(ecpm, '')::numeric)::numeric, 2) AS avg_ecpm
FROM superage.sa_airtable_sales;
```

> **Note:** CTOR (click-to-open rate) has been removed from the Revenue section. Only ECPM is displayed.

## Q35 — Subscriber Retention: Retention KPIs (all subscribers)

Retention is now aggregated across **all subscribers** (no L1/L2 split). The whole tab — KPIs, survival curve, lifespan distribution, monthly churn — uses these single-series queries.

```sql
SELECT
    COUNT(*)                                       AS total,
    COUNT(*) FILTER (WHERE state = 'Active')       AS active,
    COUNT(*) FILTER (WHERE state = 'Unsubscribed') AS churned,
    ROUND(AVG(
        EXTRACT(EPOCH FROM (date_unsubscribed - date_joined)) / 86400
    ) FILTER (
        WHERE state = 'Unsubscribed'
          AND date_unsubscribed IS NOT NULL AND date_unsubscribed::date < CURRENT_DATE
          AND date_joined       IS NOT NULL AND date_joined::date       < CURRENT_DATE
    )) AS avg_lifespan_days,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (date_unsubscribed - date_joined)) / 86400
    ) FILTER (
        WHERE state = 'Unsubscribed'
          AND date_unsubscribed IS NOT NULL AND date_unsubscribed::date < CURRENT_DATE
          AND date_joined       IS NOT NULL AND date_joined::date       < CURRENT_DATE
    )) AS median_lifespan_days
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE;
```

Exposed as `M.retention_overall` (object with `total`, `active`, `churned`, `active_rate`, `churn_rate`, `avg_lifespan_days`, `median_lifespan_days`).

## Q36 — Subscriber Retention: Lifespan Distribution (all subscribers)

```sql
SELECT
    COUNT(*) FILTER (WHERE days_active BETWEEN 0   AND 30)  AS d0_30,
    COUNT(*) FILTER (WHERE days_active BETWEEN 31  AND 90)  AS d31_90,
    COUNT(*) FILTER (WHERE days_active BETWEEN 91  AND 180) AS d91_180,
    COUNT(*) FILTER (WHERE days_active BETWEEN 181 AND 365) AS d181_365,
    COUNT(*) FILTER (WHERE days_active > 365)               AS d365plus
FROM (
    SELECT
        EXTRACT(EPOCH FROM (COALESCE(date_unsubscribed, NOW()) - date_joined)) / 86400
            AS days_active
    FROM superage.subscribers
    WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
      AND (date_unsubscribed IS NULL OR date_unsubscribed::date < CURRENT_DATE)
) x;
```

Exposed as `M.lifespan_dist = { labels: [...], data: [...] }`.

## Q37 — Subscriber Retention: Survival Curve (all subscribers)

```sql
SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 30)  AS alive_30,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 60)  AS alive_60,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 90)  AS alive_90,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 180) AS alive_180,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 365) AS alive_365
FROM (
    SELECT
        EXTRACT(EPOCH FROM (date_unsubscribed - date_joined)) / 86400 AS days_to_unsub
    FROM superage.subscribers
    WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
      AND (date_unsubscribed IS NULL OR date_unsubscribed::date < CURRENT_DATE)
) x;
```

Exposed as `M.survival_curve = { labels: ['Day 0','Day 30',...], rates: [100, %, ...] }` — single series.

## Q38 — Subscriber Retention: Monthly Churn Volume (all subscribers)

```sql
SELECT
    DATE_TRUNC('month', date_unsubscribed)::date AS month,
    COUNT(*) AS churned
FROM superage.subscribers
WHERE state = 'Unsubscribed'
  AND date_unsubscribed IS NOT NULL AND date_unsubscribed::date < CURRENT_DATE
GROUP BY 1 ORDER BY 1;
```

Exposed as `M.monthly_churn = { labels: [...], data: [...] }`.

## Q39 — Cohort Analysis: Retention Heatmap

```sql
WITH cohorts AS (
    SELECT
        DATE_TRUNC('month', date_joined::date)::date AS cohort_month,
        email,
        date_joined::date     AS joined,
        date_unsubscribed::date AS unsubbed
    FROM superage.subscribers
    WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
)
SELECT
    cohort_month,
    COUNT(*)                                                                    AS cohort_size,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined +  30)  AS alive_m1,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined +  60)  AS alive_m2,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined +  90)  AS alive_m3,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 120)  AS alive_m4,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 150)  AS alive_m5,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 180)  AS alive_m6,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 270)  AS alive_m9,
    COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 365)  AS alive_m12
FROM cohorts
GROUP BY 1 ORDER BY 1;
```

> **Reading the heatmap:** `alive_m3 / cohort_size * 100` = % still active 90 days after joining.
> Cells where `cohort_month + offset_days > CURRENT_DATE` are marked null — the cohort hasn't aged enough yet.

## Q40 — Cohort Analysis: Cohort Performance Table (with campaigns sent that month)

```sql
WITH cohorts AS (
    SELECT
        TO_CHAR(DATE_TRUNC('month', date_joined::date), 'Mon YYYY') AS cohort_label,
        DATE_TRUNC('month', date_joined::date)::date                AS cohort_month,
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE state = 'Active')       AS active_now,
        COUNT(*) FILTER (WHERE state = 'Unsubscribed') AS churned,
        COUNT(*) FILTER (WHERE unsubbed_within_90)     AS churned_90d
    FROM (
        SELECT
            date_joined,
            state,
            (
                state = 'Unsubscribed'
                AND date_unsubscribed IS NOT NULL
                AND date_unsubscribed::date <= date_joined::date + 90
            ) AS unsubbed_within_90
        FROM superage.subscribers
        WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
    ) x
    GROUP BY 1, 2
    HAVING COUNT(*) >= 20
),
camps AS (
    SELECT
        DATE_TRUNC('month', "Sent Date "::date)::date AS camp_month,
        COUNT(*) AS campaigns_sent
    FROM superage."Campaigns"
    WHERE "Sent Date " IS NOT NULL
      AND "Sent Date "::date < CURRENT_DATE
      AND "Recipients" > 1000
    GROUP BY 1
)
SELECT
    c.cohort_label,
    c.cohort_month,
    c.total,
    c.active_now,
    c.churned,
    ROUND(c.churned::numeric   / NULLIF(c.total,0) * 100, 1) AS churn_rate_pct,
    ROUND(c.active_now::numeric / NULLIF(c.total,0) * 100, 1) AS retention_pct,
    ROUND(c.churned_90d::numeric / NULLIF(c.total,0) * 100, 1) AS early_churn_pct,
    COALESCE(k.campaigns_sent, 0) AS campaigns_sent
FROM cohorts c
LEFT JOIN camps k ON k.camp_month = c.cohort_month
ORDER BY c.cohort_month;
```

## Q41 — Cohort Analysis: 90-Day Retention Rate by Acquisition Source + 90-Day Churn Rate Chart

```sql
SELECT
    COALESCE(NULLIF(TRIM(utm_source),''), NULLIF(TRIM(source),''), 'Organic') AS source,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE state = 'Active') AS active_now,
    ROUND(
        COUNT(*) FILTER (WHERE
            state = 'Active'
            OR (state = 'Unsubscribed'
                AND date_unsubscribed IS NOT NULL
                AND date_unsubscribed::date > date_joined::date + 90)
        )::numeric / NULLIF(COUNT(*),0) * 100, 1
    ) AS retention_90d_pct
FROM superage.subscribers
WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
GROUP BY 1
HAVING COUNT(*) >= 100
ORDER BY 4 DESC
LIMIT 12;
```

> `utm_source = NULL` rows are bucketed as `Organic` (no campaign attribution) and **kept** in the chart — they represent direct/organic subscribers, not missing data.

---

## Data Sources Summary

| Section | Primary Table(s) |
|---|---|
| Subscribers / Audience | `superage.subscribers` |
| Campaigns | `superage."Campaigns"` |
| Article placements | `superage.articles_clicks` |
| Website categories & tags | `superage.wordpress_articles` |
| Subscriber click totals | `superage.subscriber_clicks` |
| Longevity quiz | `superage.subscriber_quiz` |
| Revenue & sponsors | `superage.sa_airtable_sales` |
| Cohort analysis | `superage.subscribers` + `superage."Campaigns"` |

---

## Lambda Configuration

| Env Var | Default | Purpose |
|---|---|---|
| `DB_SECRET_ARN` | — | AWS Secrets Manager ARN for DB credentials |
| `GITHUB_TOKEN` | — | Fine-grained PAT with Contents read+write |
| `GITHUB_REPO` | `O-platform/retention-dshb` | Target repository |
| `GITHUB_BRANCH` | `main` | Branch to commit JSON to |
| `GITHUB_FILE_PATH` | `superage-staging/superage-metrics.json` | Path of the JSON file in the repo |
| `SA_SCHEMA` | `superage` | PostgreSQL schema name |
| `COMMIT_TO_GITHUB` | `true` | Set to `false` for local test runs |

---

## Key Implementation Notes

1. **Campaign filter**: All campaign queries require `"Recipients" > 1000` to exclude test and low-volume sends.
2. **Acquisition quality**: Only `utm_source` is used. `o_event` and `sub_source` groupings have been removed.
3. **UTM click performance**: `utm_clicks_performance` joins `subscriber_clicks` to `subscribers` to show which source drives the most article click activity (unique and total).
4. **Marital status**: Column is `marital_status` in `subscriber_quiz`.
5. **Content tab**: Called "Website Content" — categories and tags come from WordPress.
6. **No technical detail in the UI**: Column names, table names, and join logic are documented here only.
7. **WordPress URL join**: Normalized by lowercasing, stripping protocol/www, trimming slashes and query params.
8. **Categories and tags**: Both are comma-separated in WordPress; split individually before aggregating.
9. **Cohort heatmap null cells**: A cell is null when `cohort_month + offset_days > CURRENT_DATE`.
10. **Cohort UTM filter**: Only sources with 100+ subscribers included. Cohort table requires 20+ per cohort.
11. **Subscriber growth chart**: In Overview tab only. Uses `subscriber_monthly` payload (new_subs, unsubs, active_count per calendar month). Gained/lost are bar datasets on left Y axis; total active is a line on right Y axis. `net` field removed.
12. **Revenue monthly grouping**: `DATE_TRUNC('month', issue_date::date)` + `TO_CHAR(..., 'Mon YYYY')` prevents one bar per deal date.
13. **Dashboard logo**: `svg-sa.svg` in `superage-staging/`. CSS `filter: brightness(0) invert(1)` renders it white on the green badge.
14. **Dark mode default**: `<body class="dark">` on load; Chart.js initialised with dark palette.
15. **Cohort campaigns-per-month**: Joined from `superage."Campaigns"` by matching `DATE_TRUNC('month', "Sent Date "::date)` to the cohort join month. Only campaigns with `Recipients > 1000` counted.
16. **articles_clicks date filter removed**: No `created_at` filter on any `articles_clicks` query. All rows included regardless of date.
17. **games/waitlist exclusion**: `LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')` applied to content type, category, and tag breakdown queries.
18. **CTOR removed from Revenue**: Avg Issue CTOR and Avg Sponsor CTOR KPI cards removed. `avg_ctor` column removed from Top Sponsors table. Q34 now only computes `avg_ecpm`.
19. **Hidden Gems removed**: Section and query no longer used in dashboard (Q17 kept for reference only).
20. **Overview sponsor revenue removed**: Sponsor Revenue KPI card removed from Overview tab (still available in Revenue & Sponsors tab).
21. **Logo background**: `.sa-logo` badge background changed to `#000` (black).
22. **Dashboard title**: `SuperAge — Brand Pulse Dashboard`.
