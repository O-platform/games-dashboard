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

> **Canonical "Active" rule** (used wherever the dashboard reports an "Active" count — send-to KPI,
> retention KPI, retention-by-source, cohort table, 90-day source retention):
> `state = 'Active' AND engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')`. Both halves are required.

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
- `"Recipients" > 95` — excludes test sends and low-volume internal mailings

In the lambda this is stored as:
```sql
"Sent Date " IS NOT NULL
AND "Sent Date "::date < CURRENT_DATE
AND "Recipients" > 95
```

---

## 1. Overview

The overview tab surfaces the most important headline numbers across all sections.

**KPI cards — primary row** (4 cards, in this order):

| Metric | What it measures |
|---|---|
| Active (Send-to) | Subscribers we actually send to (`state='Active' AND engagement_segment NOT IN Ghosts/Zombies/Dormant`) |
| Avg Open Rate | Average unique open rate across campaigns (≥ 95 recipients) |
| Avg Click Rate | Average click rate across campaigns |
| Campaigns Sent | Count of campaigns with ≥ 95 recipients sent before today |

**KPI cards — secondary row** (4 cards, less critical):

| Metric | What it measures |
|---|---|
| Total Subscribers | All-time subscriber count (active base) |
| Unsubscribed | Count + % of total |
| Bounced | Count + % of total |
| Quiz Takers | Subscribers who completed the longevity quiz |

**Charts:**
- **Current Subscriber Mix** — Donut chart over **current subscribers only** (`state='Active'`), split by `engagement_segment`. Three slices: **Send-To** (reachable engaged), **Dormant / Ghost / Zombie** (the three disengaged segments merged into one), **Other** (null / empty / unrecognised segment). Unsubscribed / Bounced / Deleted are excluded — they're not in `state='Active'`. See Q2.
- **Campaign Performance Trend** — Line chart of open rate and click rate across the last 30 campaigns, ordered by send date.
- **Subscriber Growth Over Time** — Green bars = gained (new subscribers), red bars = lost (unsubscribes), blue line = total active subscribers at month end (right Y axis). 18-month sliding window.

**Recent Campaigns Metrics table** — **last 10 sent campaigns by date** (most recent first). Sourced client-side from `M.campaign_table` sorted by `sent_date` DESC and sliced to the first 10; no separate SQL query (the data is already loaded for the Campaigns tab). Columns: #, Campaign (linked to `url`), Sent date, Recipients, Open Rate, Click Rate. Replaces the previous "Top Campaign Open Rates" table on Overview.

---

## 2. Campaigns

All campaign metrics use the filter: `Recipients > 95 AND Sent Date < today`.

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

- **Top 15 Campaigns by Open Rate** — Horizontal bar, ranked by `UOpenRate`. Computed **client-side** from `M.campaign_table` (sourced from Q9). The lambda no longer ships a dedicated `top_campaigns` array — the dashboard re-ranks the in-scope set whenever Sunday Spotlight is toggled.
- **Open Rate vs Click Rate** — Scatter plot; each point is one campaign in the current scope.

### Campaign Table

Client-paginated table (20 rows / page) of all qualifying campaigns sorted by send date (most recent first). Driven by `campaign_table` (Q9).

### Sunday Spotlight Toggle (UI-side filter)

The toggle excludes campaigns whose `name` contains `sunday spotlight` (case-insensitive) before KPIs, charts and the paginated table are rendered. It is purely client-side — no SQL change. **Default is OFF** (toggle unchecked = "Excluding branded digest sends"), so the tab opens with Sunday Spotlight filtered out by default.

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

### Click Analysis tab — toolbar + KPIs (both react to Sunday Spotlight)

A single **toolbar sits at the very top of the tab, above the KPI cards**. It exposes the **Include Sunday Spotlight** toggle (default OFF) and a **Section 1 metric** dropdown (Total clicks / Clicks / campaign / Click-to-Open %, defaulting to *Clicks / campaign* so the headline chart isn't biased by months with more sends). The toggle scopes **the KPI cards** *and* Section 1 (campaign aggregates) *and* Section 2 (raw click events); the metric dropdown only applies to Section 1.

The four headline KPIs are computed **client-side** from `M.campaign_table` filtered by the current Sunday Spotlight scope (rather than reading static lambda-time fields), so flipping the toggle visibly changes every number on the tab in one go. `_renderClickKpis()` is called by `_refreshClicksTab()` whenever the toggle flips.

| KPI | Formula (over in-scope `campaign_table` rows) |
|---|---|
| Campaigns Sent | `COUNT(*)` |
| Total Recipients | `SUM(Recipients)` |
| Total Clicks | `SUM(Clicks)` |
| Avg Clicks / Campaign | `round(Total Clicks / Campaigns Sent)` |

In-scope filter: when **Include Sunday Spotlight** is OFF, rows whose `name` matches `sunday spotlight` (case-insensitive) are dropped; when ON, every row participates. Same predicate the Section 1 / Section 2 chart re-aggregators use, so all four cards always agree with the bars below.

> The earlier article-level KPIs (`total_article_clicks` / `unique_article_clickers` / `articles_clicked_count` / `avg_clicks_per_article` from `articles_clicks`) couldn't be filtered by Sunday Spotlight at the SQL layer — there's no campaign-name column on `articles_clicks` — so the lambda fields are no longer surfaced on this tab. They're still emitted as JSON fallbacks for any external consumer.

**Section 1 — Campaign-Level Aggregated** (charts: Weekly Campaign Clicks, Monthly Campaign Clicks) is rebuilt **client-side** from `M.campaign_table` (sourced from `superage."Campaigns"` via Q9). Each campaign is bucketed by ISO week (Mon–Sun) or calendar month based on its `sent_date`, then `clicks`, `unique_opens`, `recipients`, and campaign count are summed per period. The three metric modes are computed from these sums:

- **Total clicks** = `sum(clicks)`
- **Clicks / campaign** = `round(sum(clicks) / count(campaigns))` — removes campaign-count bias between periods (default view)
- **Click-to-Open %** = `sum(clicks) / sum(unique_opens) * 100` (1 d.p.) — normalises for engaged audience size

The "Include Sunday Spotlight" toggle simply filters out `campaign_table` rows whose `name` matches `sunday spotlight` (case-insensitive) before bucketing.

**Section 2 — Raw Click Events** (Same Weekday, Weekly, Monthly) reads `M.raw_clicks_*` produced by the comparison lambda (`superage."Campaigns_Clicks"`). Each series now ships two parallel count arrays — `clicks` (all events) and `clicks_no_ss` (excluding events whose `issue_name` matches Sunday Spotlight). The dashboard picks the array based on the toggle; if the lambda hasn't been re-run yet, it falls back to `clicks`.

**WoW / MoM comparison** under each Section 1 and Section 2 chart now compares the **last two completed periods** — `is_current[i]` bars are skipped — so an in-progress week/month no longer triggers a misleading "drop".

> **Excluded:** content types `games` and `waitlist` are excluded from type/category/tag breakdowns.

---

## 4. Audience

### Time window selector

The whole Audience tab is governed by a single **time window** toggle at the top: `All time / Last 30 days / Last 60 days / Last 90 days`. Picking a window re-renders, in one shot:

- The **Total / New Subscribers** KPI card (label flips between "Total Subscribers" + "All-time base" and "New Subscribers" + "Joined in last N days").
- The **Top Source** and **Top Source %** KPI cards (leader within the windowed cohort).
- The **Acquisition by Source** doughnut (subscribers per source within the window).
- The **Source Clicks Performance** bar chart (clicks per source — see note in Q20 on the all-time vs windowed data source).
- The **Acquisition Quality by Source** table (`rows_30d / rows_60d / rows_90d` from Q19).

The **Active Rate** card stays global ("Currently active (global)") — it's not source-scoped, so a per-window value wouldn't be meaningful. The toggle state is held in `_setAcqWindow()` and isn't persisted across reloads.

### Subscriber KPIs

Total subscribers (or in-window cohort), active rate, top source, top source share.

### Acquisition by Source

The dashboard label is **Source** (UTM is treated as an internal term in the SQL only). Grouped by the fallback `COALESCE(NULLIF(TRIM(utm_source),''), NULLIF(TRIM(source),''), 'Organic')` — `utm_source` wins, then the legacy `source` column, then the literal `Organic`. Both the pie chart and the table consume `M.acquisition_quality.utm_source.rows_all / rows_30d / rows_60d / rows_90d` (Q19).

The Audience tab table renders these columns per source: **Subscribers**, **% of Total**, **Clickers**, **Clicker Rate**, **Clicks**, **Avg Clicks / Subscriber**, **30-Day Churn**, **90-Day Churn**. (Avg Sponsor Click / Sub and Open Rate % were removed — see `FEEDBACK_REPLIES.md`.)

### Source Clicks Performance

For the **All time** window, the bar chart uses `M.utm_clicks_performance` (Q20 — `subscriber_clicks` rollup with separate unique vs total counts). For **30 / 60 / 90-day windows**, it falls back to the per-source rows from Q19 (raw `Campaigns_Clicks` events scoped to the window). Q19 only carries one click count per row (events), so the windowed view's Unique-Clicks and Total-Clicks bars are identical heights — a known trade-off documented in the lambda.

### Source label canonicalisation (source of truth)

Raw source values are normalised to a canonical display label **before** the `GROUP BY` in Q19 / Q20 / Q35b / Q37b. The mapping lives in two places that must stay in sync:

- **`_canon_source(col_sql)`** in `superage_metrics_lambda_updated.py` — SQL `CASE` statement, runs server-side so duplicate display rows collapse in the rollup itself.
- **`utmLabel(raw)`** in `index.html` — JS fallback that hits any raw value that slipped through (e.g. a brand-new source the lambda hasn't been updated for).

**Source priority chain (applied in all acquisition queries):**
`sa.acquisition_utm_source` (from `subscriber_acquisition`) → `s.utm_source` → `s.source` → `'Organic'`

The `subscriber_acquisition` table (`superage.subscriber_acquisition`) is LEFT JOINed on `LOWER(TRIM(email))` with filter `acquisition_status IN ('added', 'resubscribed')`. When a matching SA row has a non-NULL `acquisition_date`, it is used as the **effective join date** via `COALESCE(sa.acquisition_date, s.date_joined)` for lifespan and churn-window calculations.

**Taboola exclusion:** rows where the canonical label resolves to `'Taboola'` are excluded from all source-grouped result sets (Q19, Q20, Q35b, Q37b, and the comparison lambda top-source query). The canonical mapping itself is still defined so display rendering works if it ever appears in raw data.

Comparisons use `LOWER(TRIM(value))`, so case + whitespace differences collapse automatically. Anything that doesn't match a rule below falls through to `NULLIF(TRIM(value), '')` and is rendered with its raw string.

| Canonical label | Matches (lowercased) | Notes |
|---|---|---|
| **AllHealthy** | `ahcpl1`, `allhealthy`, `allhealthy.com` | AH brand family |
| **TrueDemocracy** | `tdcpl1`, `tdcpl2`, `td_cpl2*` *(LIKE prefix — every date-stamped batch)* | every `TD_CPL2_YYYYMMDD` batch rolls up here |
| **LivingSimply** | `lscpl1`, `lscpl2`, `ls_cpl2`, `livingsimply`, `livingsimply.com` | LS family |
| **DailyPuzzle** | `dpcpl1`, `dp_cpl2` | DP family |
| **HealthFirst** | `hfcpl1` | |
| **FitConnect** | `fccpl1` | |
| **Meta** | `facebook`, `meta`, `fb`, `ig` | Facebook + Instagram only; `if`/`ifcpl1` are split out below |
| **IFCPL** | `if`, `ifcpl1` | own brand — used to be folded into Meta but split out in commit `d106efe` |
| **Taboola** | `taboola` *(LOWER handles every casing)* | |
| **HealthBrief** | `healthbrief` | |
| **SuperAge Quiz** | `superagequiz`, `longevity_quiz` | distinct from `SuperAge` below |
| **TheAgeist** | `theageist`, `theageist001`, `ageist`, `ageist_*` *(LIKE prefix)*, `ageistrequest*` *(LIKE prefix)* | every sample / request / request-first-issue variant rolls up |
| **RecommendedReads** | `recommendedreads.com`, `rr_cpl2`, `rrcpl1*` *(LIKE prefix — covers `rrcpl1`, `rrcpl1002525`, …)* | |
| **Campaign Monitor** | `campaign_monitor` *(LOWER handles `Campaign_monitor`)* | |
| **Welcome Flow** | `welcome flow`, `welcome+flow` | URL-encoded variant collapsed too |
| **NNCPL** | `nncpl1`, `nn_cpl2*` *(LIKE prefix)*, `nn1_cpl2*` *(LIKE prefix)* | every NN_CPL2 batch + the oneclick variant |
| **ISCPL** | `is`, `iscpl1` | |
| **AI** | `chatgpt.com`, `perplexity`, `nbot.ai` | aggregated AI-referrer bucket |
| **Refind** | `refind` | own label (just the casing fix) |
| **SuperAge** | `superage` | own label — **distinct from SuperAge Quiz** |
| **Organic** | `''` (empty `utm_source` and `source`) | final `COALESCE` fallback in Q19's label_expr |

> Adding a new entry: add a `WHEN` branch to `_canon_source(col_sql)` in `superage_metrics_lambda_updated.py` **and** a matching `if (...) return '<label>';` block in `utmLabel(raw)` in `index.html`. Both files have cross-reference comments calling out the requirement.

The same canonicaliser also runs in **Q35b — Retention by Acquisition Source**, where the canonical buckets (Meta, AllHealthy, TrueDemocracy, IFCPL, RecommendedReads, NNCPL, ISCPL, …) plus a synthetic `Direct` bucket (for `organic` / `direct` / empty UTM) drive the per-source lifespan / unsubscribe-window table. The Q35b CASE coerces `'organic' / 'direct' / ''` to `Direct` and routes everything else through `_canon_source` — see the SQL itself or the matching insight string in `renderRetention()`.

---

## 5. Audience Persona

| Metric | What it measures |
|---|---|
| Quiz Takers | Subscribers with a completed longevity quiz |
| Avg Age | Average age of quiz takers |
| Fitness Quiz | Subscribers who completed the fitness assessment |
| Menu Quiz | Subscribers who completed the nutrition quiz |

Demographics: age distribution, gender, marital status, exercise frequency, sleep hours, education level, body weight profile. (Longevity-score buckets were removed when the tab was renamed from "Longevity Quiz" to "Audience Persona".)

---

## 6. Revenue & Sponsors

Source: `superage.sa_airtable_sales`. Every query in this section requires both **`$_line_amount IS NOT NULL`** and **`sponsor_type IS NOT NULL AND TRIM(sponsor_type) != ''`** so rows without a categorised sponsor type are excluded across the tab. Future-dated rows are **kept** — the Monthly Line Amount chart shows them as faded bars so booked-but-not-yet-run placements are visible alongside historical line amounts.

| Metric (dashboard label) | What it measures | JSON field |
|---|---|---|
| Total Line Amount | Sum of `$_line_amount` across the filtered rows | `total_revenue_fmt` |
| Total Deals | Count of sponsorship placements | `total_sponsor_deals` |
| Avg Deal Size | Average `$_line_amount` per placement | `avg_deal_size_fmt` |
| Top Sponsor | Highest-line-amount sponsor | `top_sponsors[0].name` |

> The Airtable column is `$_line_amount`, so the user-facing labels on the Revenue & Sponsors tab read **"Line Amount"** (not "Revenue") to match the source. The internal JSON fields stay named `total_revenue` / `total_revenue_fmt` for backwards-compat with anything downstream reading the metrics JSON — only the UI strings changed.

> **ECPM retired (2026-05).** The `Avg ECPM` KPI card, the dedicated `avg_ecpm` SQL (old Q34), and the `Avg ECPM` column on the Top Sponsors table were all removed. The lambda no longer emits `M.avg_ecpm` or per-row `avg_ecpm` on `top_sponsors[]`.

---

## 7. Subscriber Retention

Aggregates retention across **all subscribers** (no L1/L2 split). KPIs: total, active, churned, avg + median lifespan, active rate. **"Active"** uses the canonical two-condition rule (state='Active' AND engagement_segment NOT IN Ghosts/Zombies/Dormant).

Charts: Survival Curve (% still active at day 30 / 60 / 90 / 180 / 365), Lifespan Distribution, Monthly Churn Volume.

### Retention by Acquisition Source

A second table on the same tab buckets subscribers by `COALESCE(utm_source, source, 'Organic')` into six product-relevant labels (**AH CPL**, **Ageist CPL**, **Share**, **Meta**, **Google**, **Direct**) and reports for each:

| Column | Meaning |
|---|---|
| Subscribers | Total subscribers in the bucket |
| Active Now / Churned | State split — **Active Now** uses the canonical Active rule |
| Unsub ≤ 15 d / ≤ 30 d | Count + % of subscribers who unsubscribed within 15 / 30 days of join |
| Avg Lifespan / Median Lifespan | Mean / median lifespan in days (across churned subscribers only) |
| Clickers, Clicker Rate | Subscribers in the bucket who clicked ≥ 1 article (from `subscriber_clicks`) |
| Unique Clicks, Clicks / Clicker | Sum of unique clicks and per-clicker average |

Powered by `M.retention_by_source[]` — see Q35b for the SQL.

---

## 8. Cohort Analysis

Groups subscribers by join month and tracks % still active at M+1 through M+12.

| KPI | Source |
|---|---|
| Total Cohorts | Distinct join-month groups |
| Avg 90-Day Retention | Average M+3 retention across all cohorts with ≥ 20 subscribers |
| Best / Worst Cohort | Highest / lowest M+3 retention rate |

Charts: Retention Heatmap, 90-Day Churn Rate by Acquisition Source. (The legacy "90-Day Retention by UTM Source" chart was removed; the UTM-cohort SQL no longer runs.)

Cohort Performance Table columns: Cohort, Size, Still Active, Retention Rate, Churn Rate, Early Churn (90d), Campaigns That Month. **"Active Now" / "Retention %"** use the canonical Active rule.

---

---

# SQL Query Reference

All queries use `superage` as the schema. Replace with your `SA_SCHEMA` value as needed.

---

## Q1 — Overview / Audience: Subscriber Overview KPIs

**Definitions** (per product):
- **Total Subscribers** = `COUNT(*) WHERE state = 'Active'`. This is the active base, not the row count across all states.
- **Total (all states)** = raw `COUNT(*)` — exposed as `total_all_states` and used as the denominator for state-mix ratios (unsub %, bounced %, deleted %).
- **Active (Send-to)** = subscribers whose `state = 'Active'` AND `engagement_segment NOT IN (Ghosts, Zombies, Dormant)`. See Q1b.

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

**Definition of "Active" across the whole dashboard:**
`state = 'Active'` **AND** `engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')`.
Both halves are required — `state='Active'` alone is too broad (it includes Dormant /
Zombie / Ghost subscribers we don't send to), and the engagement filter alone is too
broad (it includes Unsubscribed / Bounced / Deleted state rows that happen to lack a
Ghost-style engagement label).

```sql
SELECT
    COUNT(*) FILTER (
        WHERE state = 'Active'
          AND engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
    ) AS send_to_active
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE;
```

Exposed as `M.send_to_active`; `send_to_rate = send_to_active / total_subscribers` (active base). **Every other "Active" count in this doc — retention KPI (Q35), cohort table (Q40), per-source retention (Q35b), 90-day source retention (Q41) — uses the same two-condition rule.**

## Q2 — Overview: Current Subscriber Mix (Donut Chart)

The Overview donut covers **current subscribers only** (`state = 'Active'`), split by `engagement_segment` so the chart answers **"of the people we still call active, how engaged are they actually?"**. Unsubscribed / Bounced / Deleted records are excluded — they're not in `state='Active'`, so they don't belong on this view.

Three slices, all computed from a single `state='Active'` row scan. The Zombies / Ghosts / Dormant segments are summed into one **Dormant / Ghost / Zombie** bucket on the donut — at the chart level they all mean the same thing ("on the list but disengaged"). The per-segment FILTER counts are still produced by the SQL so anything that needs the individual splits later can read them off the lambda row.

| Slice | Formula (state='Active' AND …) | Colour |
|---|---|---|
| **Send-To** | `engagement_segment NOT IN ('Ghosts','Zombies','Dormant')` | green `#1a7f37` |
| **Dormant / Ghost / Zombie** | `engagement_segment IN ('Ghosts','Zombies','Dormant')` *(sum of the three per-segment counts)* | amber `#9a6700` |
| **Other** | `engagement_segment IS NULL OR engagement_segment = ''` *(residual — also catches any unrecognised future segment label)* | grey `#9ca3af` |

The "Other" slice is computed as a **residual** against the active base (`total_subscribers − send_to_active − zombies − ghosts − dormant`) so any new `engagement_segment` value the data team adds rolls into it automatically rather than disappearing.

```sql
-- All five counts come from a single scan against state='Active' rows.
-- The lambda then sums zombies + ghosts + dormant into one chart slice;
-- the per-segment counts are retained internally for future use.
SELECT
    COUNT(*) FILTER (
        WHERE state = 'Active'
          AND engagement_segment NOT IN ('Ghosts','Zombies','Dormant')
    )                                                                 AS send_to,
    COUNT(*) FILTER (WHERE state = 'Active' AND engagement_segment = 'Zombies') AS zombies,
    COUNT(*) FILTER (WHERE state = 'Active' AND engagement_segment = 'Ghosts')  AS ghosts,
    COUNT(*) FILTER (WHERE state = 'Active' AND engagement_segment = 'Dormant') AS dormant,
    COUNT(*) FILTER (
        WHERE state = 'Active'
          AND (engagement_segment IS NULL OR engagement_segment = '')
    )                                                                 AS other_segment
FROM superage.subscribers
WHERE date_joined::date < CURRENT_DATE;
```

Emitted as:

```json
M.subscriber_engagement_mix = {
  "labels": ["Send-To", "Dormant / Ghost / Zombie", "Other"],
  "data":   [<send_to>, <zombies + ghosts + dormant>, <other_residual>],
  "colors": ["#1a7f37", "#9a6700", "#9ca3af"]
}
```

The legacy `M.subscriber_states` four-state JSON (`Active` / `Unsubscribed` / `Bounced` / `Deleted`, ad-hoc query below) is still emitted for backwards compatibility, but the Overview chart now reads `subscriber_engagement_mix` and only falls back to `subscriber_states` when the new field is missing (i.e. before the lambda re-runs).

```sql
-- Legacy four-state distribution (kept in JSON, no longer the primary chart)
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
- `active_count` ← `total_active` (purple line, right Y axis — the "Total Subscribers" line)

The dashboard derives `net change` client-side as `new_subs[i] - unsubs[i]` and renders it as a blue line on the left axis. `MAX(total_active)` captures the end-of-month snapshot when the table has daily rows.

> **Q5 — Subscription Level Distribution**, **Q6 — Top Acquisition Sources by Volume**, and **Q7 — US-Based Subscriber Count** were retired from the lambda. Their JSON outputs (`sub_level_dist`, `source_dist`, `us_based_count`) were never consumed by the dashboard.

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
  AND "Recipients" > 95;
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
  AND "Recipients" > 95
ORDER BY "Sent Date " ASC;
```

Feeds `campaign_table` (dashboard list, scatter chart) and `campaign_trend` (line chart).
`URL` becomes the click-through link on the campaign name in the dashboard table.

> **Q10 — Top 15 Campaigns by Open Rate** was retired from the lambda. The bar chart on the Campaigns tab is now built **client-side** from `M.campaign_table` (Q9): the dashboard sorts the in-scope rows by `UOpenRate DESC` and slices to 15 whenever the Sunday Spotlight toggle changes. The Overview tab's "Recent Campaigns Metrics" table is likewise sourced from `M.campaign_table` (most-recent 10 by `sent_date`).

## Q11 — Website Content: Article Placements KPIs

```sql
SELECT
    COUNT(*) AS placements,
    COALESCE(SUM(unique_clicks), 0)     AS unique_clicks,
    COALESCE(SUM(non_unique_clicks), 0) AS non_unique_clicks
FROM superage.articles_clicks ac;
```

> **Q12 — Content Type Performance Table** was retired from the lambda. Its JSON outputs (`content_type`, `content_type_table`) weren't consumed by the dashboard — the Content Reference tab's Content Type breakdown is now derived **client-side** from `M.content_drill_table` (Q16b), splitting on `type`.

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

> **Q14 — Articles Published per Category** was retired from the lambda. Its `article_category_counts` JSON wasn't consumed by the dashboard — Top Categories on the Content Reference tab is derived from `M.content_drill_table` (Q16b).

## Q15 — Website Content: Top Categories (avg clicks per article)

> **Where this chart's numbers actually come from**
> The "Top Categories" bar chart on the **Content Reference** tab is **not**
> populated by a dedicated category SQL query. It's computed **client-side**
> from `M.content_drill_table` (the article-level table produced by Q16b),
> by splitting each article's comma-separated `categories` field and
> counting / summing per label.
>
> The old per-category SQL below (`article_category_clicks`) still runs in
> the lambda but its output isn't consumed by the dashboard — it's a
> reporting query you can run ad-hoc in pgAdmin. Because Q16b's
> `content_drill_table` uses **`INNER JOIN`** to `wordpress_articles` and
> caps at the **top 300 articles by unique clicks**, while the legacy SQL
> uses **`LEFT JOIN`** (so unmatched URLs fall through to "Uncategorized")
> and has no per-article cap, the two queries report different article
> counts. Click totals match because the same `articles_clicks` rows back
> both. **The dashboard is the reference.**

### What the dashboard plots (client-side logic)

```text
for each article in M.content_drill_table:
    for each label in split(article.categories, ","):
        bucket[label].unique += article.unique_clicks
        bucket[label].total  += article.total_clicks
        bucket[label].count  += 1
        bucket[label].avgUnique = round(bucket[label].unique / bucket[label].count)
        bucket[label].avgTotal  = round(bucket[label].total  / bucket[label].count)

sort by avgUnique DESC, take top 10
```

The article-level rows behind `content_drill_table` come from Q16b:

```sql
-- Q16b (excerpt): INNER JOIN articles_clicks → wordpress_articles on
-- normalized URL, LIMIT 300 by unique clicks. Full query is in the
-- "Q16b — Content Reference: Article Drill-Down Table" section.
WITH ac AS (...articles_clicks rows, excluding type IN ('games','waitlist')...),
     wa AS (...wordpress_articles with COALESCE(categories,'Uncategorized')...)
SELECT ac.article_title, ac.url, ac.unique_clicks, ac.non_unique_clicks,
       ac.story_position, ac.position_category,
       wa.categories, wa.tags, wa.written_by
FROM ac
INNER JOIN wa ON ac.norm_url = wa.norm_url
ORDER BY ac.unique_clicks DESC NULLS LAST
LIMIT 300;
```

### Example dashboard output (snapshot — May 2026)

| label        | articles | avg_unique | avg_total | total_unique | total_total |
|--------------|---------:|-----------:|----------:|-------------:|------------:|
| Fitness      |       63 |      5,915 |     6,683 |      372,615 |     421,027 |
| Longevity    |      103 |      4,966 |     5,657 |      511,526 |     582,747 |
| Nutrition    |       76 |      4,569 |     5,029 |      347,262 |     382,198 |
| Focus        |       53 |      1,624 |     1,817 |       86,073 |      96,323 |
| Wealthspan   |       20 |      1,141 |     1,244 |       22,824 |      24,873 |
| Uncategorized|        1 |        ... |       ... |          ... |         ... |

> **The legacy `LEFT JOIN` category-roll-up SQL that used to live here has been retired** from the lambda along with its `article_category_clicks` JSON output. If you need to spot-check the long-tail (the rows that fell into the "Uncategorized" bucket because `articles_clicks.url` had no WordPress match), use the orphan-clicks query in **Q15b** below — it surfaces the same set without pre-rolling them into a category.

---

## Q15b — URL normalization helper (debugging join mismatches)

`articles_clicks` and `wordpress_articles` store the same article under
slightly different URL spellings:

```text
wordpress_articles.article_url  →  https://superage.com/do-pets-actually-help-you-live-longer/
articles_clicks.url             →  https://superage.com/do-pets-actually-help-you-live-longer
```

Trailing slash, `www.` prefix, query string and protocol case all vary
between the two systems. Every join in this lambda runs both columns
through the same normalizer before matching:

```sql
REGEXP_REPLACE(
    LOWER(
        TRIM(BOTH '/' FROM SPLIT_PART(<url-column>, '?', 1))
    ),
    '^https?://(www[.])?', ''
) AS norm_url
```

It (a) strips the query string after `?`, (b) lowercases, (c) trims any
leading/trailing slashes, and (d) removes the scheme + optional `www.`
prefix. So both example URLs collapse to:

```text
superage.com/do-pets-actually-help-you-live-longer
```

### Find an article by URL pattern (both tables)

Useful when you have a slug and want to see how it appears on each side:

```sql
-- Find every articles_clicks row for a URL pattern
SELECT id, issue_name, type, story_position, position_category,
       url, unique_clicks, non_unique_clicks
FROM superage.articles_clicks
WHERE LOWER(url) LIKE '%superage.com/do-pets-actually-help-you-live-longer%'
ORDER BY unique_clicks DESC;

-- Find the matching wordpress_articles row(s)
SELECT article_url, categories, tags, written_by,
       published_date, modified_date
FROM superage.wordpress_articles
WHERE LOWER(article_url) LIKE '%superage.com/do-pets-actually-help-you-live-longer%'
ORDER BY modified_date DESC NULLS LAST;
```

Generic version (replace `<slug>`):

```sql
SELECT 'articles_clicks' AS source, url AS article_url, NULL::text AS categories
FROM superage.articles_clicks
WHERE LOWER(url) LIKE '%superage.com/' || LOWER('<slug>') || '%'
UNION ALL
SELECT 'wordpress_articles' AS source, article_url, categories
FROM superage.wordpress_articles
WHERE LOWER(article_url) LIKE '%superage.com/' || LOWER('<slug>') || '%';
```

### Articles in WordPress but never clicked (no matching `articles_clicks` row)

```sql
WITH wa AS (
    SELECT
        article_url,
        categories,
        published_date,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.wordpress_articles
    WHERE (published_date IS NULL OR published_date::date < CURRENT_DATE)
), ac AS (
    SELECT DISTINCT
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.articles_clicks
)
SELECT wa.article_url, wa.categories, wa.published_date
FROM wa
LEFT JOIN ac ON wa.norm_url = ac.norm_url
WHERE ac.norm_url IS NULL
ORDER BY wa.published_date DESC NULLS LAST;
```

### `articles_clicks` rows with no matching WordPress article (orphan clicks)

These end up as the "Uncategorized" bucket in the legacy Q15 query and
are excluded from the dashboard because Q16b uses `INNER JOIN`. Run this
when you want to see what's leaking through:

```sql
WITH ac AS (
    SELECT
        url, article_title, type, unique_clicks, non_unique_clicks,
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.articles_clicks
    WHERE LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')
), wa AS (
    SELECT DISTINCT
        REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))),
                       '^https?://(www[.])?', '') AS norm_url
    FROM superage.wordpress_articles
)
SELECT
    ac.url, ac.article_title, ac.type,
    ac.unique_clicks, ac.non_unique_clicks
FROM ac
LEFT JOIN wa ON ac.norm_url = wa.norm_url
WHERE wa.norm_url IS NULL
ORDER BY ac.unique_clicks DESC NULLS LAST
LIMIT 100;
```

Common reasons a row shows up here:

- Sponsored or affiliate placements whose URL doesn't live on `superage.com`.
- Articles deleted or renamed in WordPress (the slug changed but
  `articles_clicks` still has the old send-time URL).
- Tracking-parameter remnants that survive the `SPLIT_PART(url,'?',1)` step
  (e.g. fragment-encoded suffixes `#…`).
- `type='games'` / `type='waitlist'` placements — explicitly excluded
  upstream but worth eyeballing if the count looks high.

---

## Q16 — Website Content: Top Tags (avg clicks per article)

Same situation as Q15: the **Top Tags** chart on the Content Reference tab is computed **client-side** from `M.content_drill_table` (splitting each row's comma-separated `tags`). The legacy `LEFT JOIN` tag-roll-up SQL (`article_tag_clicks`) has been retired from the lambda. There's also a retired `article_writer_clicks` query that did the same thing for `written_by`; the **Top Authors** chart on the Content Reference tab is similarly derived client-side from `content_drill_table`.

## Q16c–Q16i — Click Analysis trend queries (MOVED)

The four campaign-aggregate trend queries and the three raw-click-event
trend queries that power the **Click Analysis** tab are produced by the
**comparison lambda** (`superage_comparison_lambda.py`), not this lambda.
See [`COMPARISON_METRICS.md`](./COMPARISON_METRICS.md) for the SQL and JSON
shape. The dashboard fetches `superage-comparison.json` alongside
`superage-metrics.json` and merges them client-side.

## Q16b — Content Reference: Article Drill-Down Table (content_drill_table)

Source of the **Content Reference** tab. One row per `articles_clicks` placement, **inner-joined** to the most-recently-modified `wordpress_articles` row that shares the same normalized URL. The inner join is intentional: `articles_clicks` also records sponsor placements that are not WordPress articles — those are out of scope for this view. Returns up to 300 rows.

`categories` and `tags` are returned as the raw **comma-separated strings** stored in WordPress (e.g. `"longevity, health"`). The dashboard splits them on comma at render time to build per-value filter dropdowns, match individual categories/tags, and to aggregate the **Top Categories** and **Top Tags** charts. `written_by` is single-valued — used as-is. `position_category` (`high` / `medium` / `low`) is sourced directly from `articles_clicks` and drives the dashboard's position-category filter, position-category chart, and "Sleeper Hits" insight.

```sql
WITH ac AS (
    SELECT
        article_title, url, issue_name, issue_date,
        unique_clicks, non_unique_clicks,
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
    ac.issue_name,
    ac.issue_date,
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

**Dashboard filters** (client-side, all scope the whole tab — KPIs, position-category chart, Top Categories chart, Top Tags chart, sleeper-hits insight, table+pagination):
- Position Cat: exact match on `position_category` (`high` / `medium` / `low`).
- Position: exact match on `story_position`; `No position` selects rows where `story_position` is null/0.
- Author: exact match on `written_by`.
- Category: row matches if `categories.split(',').map(trim)` contains the selected value.
- Tag: row matches if `tags.split(',').map(trim)` contains the selected value.
- Title search: case-insensitive substring match.

**Sleeper Hits** insight: top 10 rows with `position_category = 'low'` ordered by `unique_clicks DESC` — surfaces articles placed near the bottom of an issue that nonetheless drew strong engagement.

**Clicks by Position Category chart** (top of the grid, vertical bars): grouped bars per `high` / `medium` / `low` position bucket plot **avg unique clicks per article** (green) and **avg total clicks per article** (blue) — values are `sum / count` for the in-scope rows in each bucket, formatted to 1 decimal place. Article count for each bucket is drawn above the green bar via an inline plugin (`countLabels-poscat`), and the tooltip lists absolute totals (`unique`, `total`) plus the article count. The insight strip below echoes the averages alongside the colour-coded bucket name.

**Top Categories / Top Tags charts** (client-side, no extra SQL): the dashboard takes the current filter-scoped row set, splits each row's `categories` / `tags` string on comma, and sums `unique_clicks` and `total_clicks` per label. The horizontal bar charts plot **avg clicks per article** — paired bars for `round(unique_clicks / count)` and `round(total_clicks / count)` — and surface the **top 10 labels by avg unique clicks per article**. Each bar is annotated inline with `N articles` at its right end (drawn by an inline Chart.js plugin) so the user can see how many articles back each average; the tooltip lists the absolute totals (`unique`, `total`) and the article count. Both charts re-render on every filter change via `_crRenderTopBar()` and replaced the prior **Top Position Cat** KPI card (whose info is already covered by the position-category filter and chart).

**Top Tags "Tag appears in ≥ N articles" toolbar** (Top Tags only): a five-button group above the chart (`1 / 3 / 5 / 7 / 10`) drops tags whose article count is below the threshold **before** the top-10 ranking is computed. Default is `1` (no filter). The filter is layered on top of the in-scope row set, so it composes with the other Content Reference filters (Position Cat / Author / Category / Tag / Title search). The threshold is stored in `window._crTagMin` and `_crSetTagMin()` re-renders only the Tags chart — Top Categories is unaffected. Resets to `1` on page reload.

---

## Q18 — Audience: Total Article Clickers

The full bucketed click-distribution query (1 / 2–5 / 6–10 / 11–20 / 20+) was retired because the dashboard never plotted those buckets. The lambda now only runs the simple total to feed the "Unique Clickers" KPI on the Click Analysis tab:

```sql
SELECT COUNT(*) AS total_clickers
FROM superage.subscriber_clicks;
```

Feeds `M.total_article_clickers`.

## Q19 — Audience: Acquisition Quality by Source (Engagement Table)

The "Acquisition Quality by Source" table on the Audience tab supports a
time-window selector (All / 30d / 60d / 90d). The lambda runs this query
**four times** — once with no date filter and once for each rolling window
— and ships the results as `rows_all`, `rows_30d`, `rows_60d`, `rows_90d`
inside `M.acquisition_quality.utm_source`. The dashboard's window buttons
just pick which array to render.

Click stats now come from the **raw `Campaigns_Clicks` events** (joined to
subscribers by lowercased `email_address`) so the date filter actually
scopes click activity. The old query joined the date-less
`subscriber_clicks` rollup and couldn't be windowed.

```sql
-- Label priority: sa.acquisition_utm_source >> s.utm_source >> s.source >> 'Organic'
-- Effective join date: COALESCE(sa.acquisition_date, s.date_joined).
-- Taboola rows excluded (WHERE label != 'Taboola').
-- since_days: None (all-time), 30, 60, or 90.
WITH sa_acq AS (
    SELECT
        LOWER(TRIM(email))    AS email,
        acquisition_utm_source,
        acquisition_date::date AS acquisition_date
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added', 'resubscribed')
),
s AS (
    SELECT
        LOWER(TRIM(s.email))  AS email,
        COALESCE(
            _canon(sa.acquisition_utm_source),
            _canon(s.utm_source),
            _canon(s.source),
            'Organic'
        )                      AS label,
        s.state,
        COALESCE(sa.acquisition_date, s.date_joined::date) AS eff_date,
        s.date_unsubscribed::date                          AS unsubbed
    FROM superage.subscribers s
    LEFT JOIN sa_acq sa ON sa.email = LOWER(TRIM(s.email))
    WHERE s.email IS NOT NULL AND TRIM(s.email) != ''
      AND s.date_joined::date < CURRENT_DATE
      -- AND COALESCE(sa.acquisition_date, s.date_joined::date)
      --     >= CURRENT_DATE - INTERVAL '<N> days'
),
cc AS (
    SELECT
        LOWER(TRIM(cc."EmailAddress ")) AS email,
        COUNT(*)                         AS clicks
    FROM superage."Campaigns_Clicks" cc
    WHERE cc."Date" IS NOT NULL
      AND cc."EmailAddress " IS NOT NULL
      AND TRIM(cc."EmailAddress ") != ''
      -- AND cc."Date"::date >= CURRENT_DATE - INTERVAL '<N> days'
    GROUP BY 1
)
SELECT
    s.label,
    COUNT(*)                                        AS subscribers,
    COUNT(cc.email)                                 AS clickers,
    COALESCE(SUM(cc.clicks), 0)                     AS clicks,
    ROUND(COALESCE(SUM(cc.clicks), 0)::numeric
          / NULLIF(COUNT(*), 0), 2)                 AS avg_clicks_per_subscriber,
    ROUND(COUNT(cc.email)::numeric
          / NULLIF(COUNT(*), 0) * 100, 1)           AS clicker_rate,
    COUNT(*) FILTER (
        WHERE s.state = 'Unsubscribed'
          AND s.unsubbed IS NOT NULL AND s.eff_date IS NOT NULL
          AND (s.unsubbed - s.eff_date) <= 30
    )                                               AS churned_30d,
    COUNT(*) FILTER (
        WHERE s.state = 'Unsubscribed'
          AND s.unsubbed IS NOT NULL AND s.eff_date IS NOT NULL
          AND (s.unsubbed - s.eff_date) <= 90
    )                                               AS churned_90d
FROM s LEFT JOIN cc ON s.email = cc.email
WHERE s.label != 'Taboola'
GROUP BY 1
ORDER BY subscribers DESC NULLS LAST
LIMIT 12;
```

**JSON output** (`M.acquisition_quality.utm_source`):

- `rows_all`, `rows_30d`, `rows_60d`, `rows_90d` — per-window arrays of `{label, subscribers, clickers, clicks, avg_clicks_per_subscriber, clicker_rate, churned_30d, churned_30d_rate, churned_90d, churned_90d_rate, sponsor_clicks_per_subscriber, open_rate}`. `sponsor_clicks_per_subscriber` and `open_rate` ship as `null` placeholders — they're rendered as `—` in the dashboard until the schema supports them (see "Open Rate" and "Sponsor click attribution" notes in the FEEDBACK_REPLIES).
- `rows` — alias of `rows_all` for backwards compatibility with the older HTML.
- `labels[]`, `subscribers[]`, `clickers[]`, `unique_clicks[]`, `non_unique_clicks[]`, `avg_unique_clicks_per_subscriber[]`, `clicker_rate[]` — top-level legacy arrays kept so the existing Audience pie chart keeps working without changes.

> **Caveats**
> - `clicks` is a count of **raw click events** within the window. The legacy "unique_clicks / non_unique_clicks" distinction came from the pre-aggregated `subscriber_clicks` rollup, which no longer participates in this query; both legacy fields now mirror the windowed event count.
> - For a 30-day cohort the 90-day-churn column is "not fully observable" — those subscribers haven't been around for 90 days. The number is still meaningful (it's the % who churned within 30 days of joining, capped by however long they've been around), but it tends to under-report for short windows.

## Q20 — Audience: Source Clicks Performance

```sql
-- Label priority: sa.acquisition_utm_source >> s.utm_source >> s.source >> 'Organic'.
-- Taboola excluded. All-time; windowed views fall back to Q19.
WITH sa_acq AS (
    SELECT LOWER(TRIM(email)) AS email, acquisition_utm_source
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added', 'resubscribed')
),
labeled AS (
    SELECT
        COALESCE(
            _canon(sa.acquisition_utm_source),
            _canon(s.utm_source),
            _canon(s.source),
            'Organic'
        )                             AS label,
        sc.email_address,
        sc.unique_clicks,
        sc.non_unique_clicks
    FROM superage.subscriber_clicks sc
    JOIN superage.subscribers s
      ON LOWER(TRIM(s.email)) = LOWER(TRIM(sc.email_address))
    LEFT JOIN sa_acq sa ON sa.email = LOWER(TRIM(s.email))
)
SELECT
    label,
    COUNT(DISTINCT email_address)          AS clickers,
    COALESCE(SUM(unique_clicks), 0)        AS unique_clicks,
    COALESCE(SUM(non_unique_clicks), 0)    AS total_clicks,
    ROUND(COALESCE(SUM(unique_clicks), 0)::numeric
          / NULLIF(COUNT(DISTINCT email_address), 0), 1) AS avg_per_clicker
FROM labeled
WHERE label != 'Taboola'
GROUP BY 1
ORDER BY unique_clicks DESC NULLS LAST
LIMIT 12;
```

## Q21 — Audience Persona: Quiz KPIs

```sql
SELECT
    COUNT(*)                       AS n,
    ROUND(AVG(age)::numeric, 1)    AS avg_age
FROM superage.subscriber_quiz
WHERE longevity_score IS NOT NULL;
```

> The previous version of this query also returned `avg_score` / `min_score` /
> `max_score`. Those columns were dropped when the "Audience Persona" tab
> stopped surfacing longevity-score visuals (see the FEEDBACK_REPLIES entry
> on "Audience Persona"). The `WHERE longevity_score IS NOT NULL` clause is
> kept as a completed-quiz gate.

## Q22 — Audience Persona: Age Distribution

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

## Q23 — Audience Persona: Gender Distribution

```sql
SELECT COALESCE(NULLIF(gender, ''), 'Unknown') AS gender, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC;
```

## Q25 — Audience Persona: Exercise Frequency

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

## Q26 — Audience Persona: Sleep Hours

```sql
SELECT COALESCE(NULLIF(sleep_hours, ''), 'Unknown') AS sleep, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC;
```

## Q27 — Audience Persona: Education Level

```sql
SELECT COALESCE(NULLIF(education_level, ''), 'Unknown') AS label, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC LIMIT 12;
```

## Q28 — Audience Persona: Marital Status

```sql
SELECT COALESCE(NULLIF(marital_status, ''), 'Unknown') AS label, COUNT(*) AS cnt
FROM superage.subscriber_quiz
GROUP BY 1 ORDER BY 2 DESC LIMIT 12;
```

## Q29 — Audience Persona: Body Weight Profile

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

**Future-month treatment (dashboard-side):** months whose `month_label` parses to a date strictly later than the current calendar month are rendered with **red bars** (`rgba(207,34,46,0.55)`) instead of the past/current gold (`#7d4e00`) and a dashed line segment (deals). Past + current months render solid gold. A **vertical dashed separator line** is drawn between the last past/current month and the first future month, labelled `Future →`, via an inline Chart.js plugin (`futureSeparator`). The split is computed client-side from the same array — the lambda emits the full series without partitioning.

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

> **Q34 — Avg ECPM** was retired (2026-05). The dedicated `AVG(NULLIF(ecpm,'')::numeric)` query, the `M.avg_ecpm` JSON field, and the per-row `avg_ecpm` on the Top Sponsors table are all gone. The Revenue tab still shows Total Revenue / Total Deals / Avg Deal Size / Top Sponsor; ECPM was deemed too noisy at the per-sponsor level given the small deal counts.

## Q35 — Subscriber Retention: Retention KPIs (all subscribers)

Retention is now aggregated across **all subscribers** (no L1/L2 split). The whole tab — KPIs, survival curve, lifespan distribution, monthly churn — uses these single-series queries.

```sql
SELECT
    COUNT(*)                                       AS total,
    COUNT(*) FILTER (
        WHERE state = 'Active'
          AND engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
    )                                              AS active,
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

`active` uses the two-condition Active definition from Q1b. Exposed as
`M.retention_overall` (object with `total`, `active`, `churned`, `active_rate`,
`churn_rate`, `avg_lifespan_days`, `median_lifespan_days`).

## Q35b — Subscriber Retention: Retention by Acquisition Source

Buckets every subscriber by the **canonical source label** (§4 → "Source label
canonicalisation"), then computes Lifespan (avg + median days from join to unsub, across churned subscribers only), early-unsub
counts at 15 / 30 days, and click activity from `subscriber_clicks`.
`active_now` uses the canonical Active rule. The previous fixed six-bucket
list (`AH CPL` / `Ageist CPL` / `Share` / `Meta` / `IFCPL` / `Google` /
`Direct`) was replaced — buckets now match the Audience tab exactly, with
`'organic' / 'direct' / ''` still forced to **Direct** rather than being
returned as raw values.

```sql
-- Label priority: sa.acquisition_utm_source >> s.utm_source >> s.source >> 'Organic'.
-- Effective join date: COALESCE(sa.acquisition_date, s.date_joined).
-- Taboola excluded (WHERE m.bucket != 'Taboola'). Min cohort 100; top 15 by size.
WITH sa_acq AS (
    SELECT
        LOWER(TRIM(email))    AS email,
        acquisition_utm_source,
        acquisition_date::date AS acquisition_date
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added', 'resubscribed')
),
s AS (
    SELECT
        LOWER(TRIM(sub.email))                              AS email,
        COALESCE(sa.acquisition_date, sub.date_joined::date) AS eff_joined,
        sub.date_unsubscribed::date                         AS unsubbed,
        sub.state,
        sub.engagement_segment,
        COALESCE(
            NULLIF(TRIM(sa.acquisition_utm_source), ''),
            NULLIF(TRIM(sub.utm_source), ''),
            NULLIF(TRIM(sub.source), ''),
            'Organic'
        )                                                   AS source_raw
    FROM superage.subscribers sub
    LEFT JOIN sa_acq sa ON sa.email = LOWER(TRIM(sub.email))
    WHERE sub.date_joined IS NOT NULL AND sub.date_joined::date < CURRENT_DATE
),
mapped AS (
    SELECT
        s.*,
        CASE
            WHEN LOWER(source_raw) IN ('organic', 'direct', '') THEN 'Direct'
            ELSE COALESCE(_canon(source_raw), 'Direct')
        END AS bucket,
        CASE WHEN unsubbed IS NOT NULL THEN (unsubbed - eff_joined) END AS lifespan_days
    FROM s
),
clicks AS (
    SELECT LOWER(TRIM(email_address)) AS email,
           SUM(unique_clicks)         AS unique_clicks
    FROM superage.subscriber_clicks
    WHERE email_address IS NOT NULL AND TRIM(email_address) != ''
    GROUP BY 1
)
SELECT
    m.bucket,
    COUNT(*)                                              AS subscribers,
    COUNT(*) FILTER (
        WHERE m.state = 'Active'
          AND m.engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
    )                                                     AS active_now,
    COUNT(*) FILTER (WHERE m.state = 'Unsubscribed')      AS churned,
    COUNT(*) FILTER (
        WHERE m.state = 'Unsubscribed' AND m.lifespan_days IS NOT NULL AND m.lifespan_days <= 15
    )                                                     AS unsub_15d,
    COUNT(*) FILTER (
        WHERE m.state = 'Unsubscribed' AND m.lifespan_days IS NOT NULL AND m.lifespan_days <= 30
    )                                                     AS unsub_30d,
    ROUND(AVG(m.lifespan_days) FILTER (WHERE m.lifespan_days IS NOT NULL)::numeric, 1) AS avg_lifespan_days,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.lifespan_days)
          FILTER (WHERE m.lifespan_days IS NOT NULL)::numeric, 1)                       AS median_lifespan_days,
    COALESCE(SUM(c.unique_clicks), 0)                     AS total_unique_clicks,
    COUNT(c.email)                                        AS clickers
FROM mapped m
LEFT JOIN clicks c ON c.email = m.email
WHERE m.bucket != 'Taboola'
GROUP BY m.bucket
HAVING COUNT(*) >= 100
ORDER BY subscribers DESC
LIMIT 15;
```

JSON output: `M.retention_by_source[]` with keys `source`, `subscribers`,
`active_now`, `churned`, `unsub_15d`, `unsub_15d_rate`, `unsub_30d`,
`unsub_30d_rate`, `avg_lifespan_days`, `median_lifespan_days`,
`total_unique_clicks`, `clickers`, `clicker_rate`, `avg_clicks_per_clicker`.

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

## Q37b — Subscriber Retention: Survival Curve per Acquisition Source

Same Day 0/30/60/90/180/365 shape as Q37 but split by acquisition-source bucket. Powers the new overlay on the Retention tab: the all-subscriber baseline (filled green, Q37) plus one thin line per source. Source buckets use the **canonical mapping** (§4 → "Source label canonicalisation") so labels here match the Audience tab and Q35b. Minimum cohort 100 subscribers; **no `LIMIT`** — every canonical bucket with a non-trivial cohort ships as a series so the user can decide which to keep visible. The dashboard exposes per-source toggling via the Chart.js legend (click to hide / show) plus **Show all / Hide all** buttons above the chart.

```sql
-- Label priority: sa.acquisition_utm_source >> s.utm_source >> s.source.
-- Effective join date: COALESCE(sa.acquisition_date, s.date_joined).
-- Taboola excluded. Sorted by 365-day survival rate DESC.
WITH sa_acq AS (
    SELECT
        LOWER(TRIM(email))    AS email,
        acquisition_utm_source,
        acquisition_date::date AS acquisition_date
    FROM superage.subscriber_acquisition
    WHERE acquisition_status IN ('added', 'resubscribed')
),
s AS (
    SELECT
        CASE
            WHEN LOWER(COALESCE(
                    NULLIF(TRIM(sa.acquisition_utm_source),''),
                    NULLIF(TRIM(sub.utm_source),''),
                    NULLIF(TRIM(sub.source),''), '')) IN ('organic','direct','') THEN 'Direct'
            ELSE COALESCE(
                _canon(COALESCE(NULLIF(TRIM(sa.acquisition_utm_source),''),
                                NULLIF(TRIM(sub.utm_source),''),
                                NULLIF(TRIM(sub.source),''))),
                'Direct'
            )
        END AS bucket,
        EXTRACT(EPOCH FROM (
            sub.date_unsubscribed::date
            - COALESCE(sa.acquisition_date, sub.date_joined::date)
        )) / 86400 AS days_to_unsub
    FROM superage.subscribers sub
    LEFT JOIN sa_acq sa ON sa.email = LOWER(TRIM(sub.email))
    WHERE sub.date_joined IS NOT NULL AND sub.date_joined::date < CURRENT_DATE
      AND (sub.date_unsubscribed IS NULL OR sub.date_unsubscribed::date < CURRENT_DATE)
)
SELECT
    bucket,
    COUNT(*)                                                                AS total,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 30)     AS alive_30,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 60)     AS alive_60,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 90)     AS alive_90,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 180)    AS alive_180,
    COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 365)    AS alive_365
FROM s
WHERE bucket != 'Taboola'
GROUP BY 1
HAVING COUNT(*) >= 100
ORDER BY
    (COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 365)::numeric
     / NULLIF(COUNT(*), 0)) DESC NULLS LAST,
    total DESC;
```

Exposed as:

```json
M.survival_curve_by_source = {
  "labels": ["Day 0","Day 30","Day 60","Day 90","Day 180","Day 365"],
  "series": [
    { "label": "<Source>", "total": <int>, "rates": [100.0, %30, %60, %90, %180, %365] },
    …
  ]
}
```

## Q38 — Subscriber Retention: Monthly Churn Volume + Churn % of Sends

Bar chart (left axis) plots the count of subscribers who unsubscribed in each calendar month. A blue **Churn % of sends** line (right axis) overlays it, computed as **unsubscribes ÷ total emails sent that month × 100** — the share of the audience we lost per send-volume unit. A falling line while volume bars grow is the "healthy" signal: send volume is scaling without proportional unsubscribe damage. Months with no qualifying send (`Recipients > 95`) show a bar but no point on the line (avoids divide-by-zero).

```sql
WITH unsubs AS (
    SELECT
        DATE_TRUNC('month', date_unsubscribed)::date AS month,
        COUNT(*) AS churned
    FROM superage.subscribers
    WHERE state = 'Unsubscribed'
      AND date_unsubscribed IS NOT NULL
      AND date_unsubscribed::date < CURRENT_DATE
    GROUP BY 1
),
sends AS (
    SELECT
        DATE_TRUNC('month', "Sent Date "::date)::date AS month,
        SUM("Recipients") AS total_sent,
        COUNT(*)          AS campaigns
    FROM superage."Campaigns"
    WHERE "Sent Date " IS NOT NULL
      AND "Sent Date "::date < CURRENT_DATE
      AND "Recipients" > 95
    GROUP BY 1
)
SELECT
    COALESCE(u.month, s.month)                                      AS month,
    COALESCE(u.churned, 0)                                          AS churned,
    COALESCE(s.total_sent, 0)                                       AS total_sent,
    COALESCE(s.campaigns, 0)                                        AS campaigns,
    CASE
        WHEN COALESCE(s.total_sent, 0) = 0 THEN NULL
        ELSE ROUND(COALESCE(u.churned, 0)::numeric / s.total_sent * 100, 4)
    END                                                             AS churn_pct
FROM unsubs u
FULL OUTER JOIN sends s ON u.month = s.month
ORDER BY 1;
```

Exposed as:

```json
M.monthly_churn = {
  "labels":     ["YYYY-MM-01", ...],
  "data":       [<churned per month>],
  "total_sent": [<sum of Recipients per month, 0 if no qualifying send>],
  "campaigns":  [<count of qualifying campaigns per month>],
  "churn_pct":  [<churned / total_sent * 100, or null when no sends>]
}
```

> **Why total_sent rather than total_subscribers?** A small dedicated 20K-recipient send can generate 200 unsubs (1%) while a 1M-recipient send can produce 10K unsubs (1%). Both should read as the same churn rate. Aggregating *all* unsubscribes and dividing by *all emails sent* in the month gives the per-impression damage signal regardless of how the month's volume was distributed across campaigns.
>
> **Caveat: `Recipients` on `Campaigns` is per-send, NOT deduplicated across campaigns.** The same subscriber appears in `Recipients` once per campaign they received, so `SUM(Recipients)` is a total of **email send events / impressions** — not unique people reached. Two campaigns each sent to the same 100K subscribers contributes 200K to the denominator, not 100K. That's intentional for this churn-rate signal: damage per email impression, so a high-cadence month with many campaigns isn't unfairly favoured over a low-cadence one. If you ever need unique-reach denominators instead you'd have to build a per-subscriber send table (e.g. join `Campaigns_Clicks` events back to `subscribers` and `COUNT(DISTINCT email)` per month).

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
> Note: "alive" here means *not yet unsubscribed* — i.e. lifespan-style survival. The
> per-cohort `active_now` figure in the cohort *table* (Q40) is stricter — it uses the
> canonical Active rule (state='Active' AND engagement_segment NOT IN Ghosts/Zombies/Dormant).

## Q40 — Cohort Analysis: Cohort Performance Table (2025+ cohorts)

Restricted to cohorts where `date_joined >= '2025-01-01'` so the table focuses on recent acquisition quality. Each row exposes three primary counts derived from the same single scan plus a few rates:

| Column | What it counts | Definition |
|---|---|---|
| **Cohort Size** | Subscribers who joined this month | `COUNT(*)` over the cohort |
| **Total Subscribers** | Still on the list today | `COUNT(*) FILTER (WHERE state IN ('Active','Bounced'))` |
| **Active (Send-To)** | Reachable + engaged | `COUNT(*) FILTER (WHERE state = 'Active' AND engagement_segment NOT IN ('Ghosts','Zombies','Dormant'))` |
| Active % (of Cohort Size) | Active (Send-To) / Cohort Size — canonical Active rule | derived |
| Churn % | Unsubscribed / Cohort Size | derived |
| Early Churn (90d) | Unsubscribed within 90 days of joining / Cohort Size | derived |
| Campaigns Sent | Qualifying campaigns sent during the cohort's join month | `Campaigns` table, `Recipients > 95` |

The earlier heatmap (Q39) still includes pre-2025 cohorts; this table is the only place the cut-off applies.

```sql
WITH cohorts AS (
    SELECT
        TO_CHAR(DATE_TRUNC('month', date_joined::date), 'Mon YYYY') AS cohort_label,
        DATE_TRUNC('month', date_joined::date)::date                AS cohort_month,
        COUNT(*)                                                    AS total,
        COUNT(*) FILTER (WHERE state IN ('Active', 'Bounced'))      AS total_subscribers,
        COUNT(*) FILTER (
            WHERE state = 'Active'
              AND engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
        )                                                           AS active_now,
        COUNT(*) FILTER (WHERE state = 'Unsubscribed')              AS churned,
        COUNT(*) FILTER (WHERE unsubbed_within_90)                  AS churned_90d
    FROM (
        SELECT
            date_joined,
            state,
            engagement_segment,
            (
                state = 'Unsubscribed'
                AND date_unsubscribed IS NOT NULL
                AND date_unsubscribed::date <= date_joined::date + 90
            ) AS unsubbed_within_90
        FROM superage.subscribers
        WHERE date_joined IS NOT NULL
          AND date_joined::date < CURRENT_DATE
          AND date_joined::date >= DATE '2025-01-01'
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
      AND "Recipients" > 95
    GROUP BY 1
)
SELECT
    c.cohort_label,
    c.cohort_month,
    c.total,
    c.total_subscribers,
    c.active_now,
    c.churned,
    ROUND(c.active_now::numeric / NULLIF(c.total,0) * 100, 1) AS active_pct,
    ROUND(c.churned::numeric           / NULLIF(c.total,0) * 100, 1) AS churn_rate_pct,
    ROUND(c.churned_90d::numeric       / NULLIF(c.total,0) * 100, 1) AS early_churn_pct,
    COALESCE(k.campaigns_sent, 0)                                    AS campaigns_sent
FROM cohorts c
LEFT JOIN camps k ON k.camp_month = c.cohort_month
ORDER BY c.cohort_month;
```

> **Q41 — Cohort Analysis: 90-Day Retention Rate by Acquisition Source** was retired from the lambda. The `cohort_utm_retention` JSON it emitted was never consumed by the dashboard (the per-source 90-day rate the Cohort tab actually plots comes from the Retention by Acquisition Source table — see Q35b).

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

1. **Campaign filter**: All campaign queries require `"Recipients" > 95` to exclude test and low-volume sends.
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
15. **Cohort campaigns-per-month**: Joined from `superage."Campaigns"` by matching `DATE_TRUNC('month', "Sent Date "::date)` to the cohort join month. Only campaigns with `Recipients > 95` counted.
16. **articles_clicks date filter removed**: No `created_at` filter on any `articles_clicks` query. All rows included regardless of date.
17. **games/waitlist exclusion**: `LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')` applied to content type, category, and tag breakdown queries.
18. **CTOR removed from Revenue**: Avg Issue CTOR and Avg Sponsor CTOR KPI cards removed. `avg_ctor` column removed from Top Sponsors table. Q34 now only computes `avg_ecpm`.
19. **Hidden Gems removed**: Section, query, and the `low_position_winners` JSON field are all gone (Q17 deleted from this doc).
20. **Overview sponsor revenue removed**: Sponsor Revenue KPI card removed from Overview tab (still available in Revenue & Sponsors tab).
21. **Logo background**: `.sa-logo` badge background changed to `#000` (black).
22. **Dashboard title**: `SuperAge — Brand Pulse Dashboard`.
23. **Canonical "Active" rule**: Every "Active" count in the dashboard uses `state = 'Active' AND engagement_segment NOT IN ('Ghosts','Zombies','Dormant')`. Applies to Q1b (send_to_active), Q35 (retention KPI), Q35b (retention_by_source), Q40 (cohort table) and Q41 (90-day retention by source).
24. **Top Position Cat KPI removed; Top Categories + Top Tags charts added**: The Content Reference tab no longer renders the "Top Position Cat" KPI card (position-category info is already covered by the position-category filter and chart). Two new horizontal bar charts — **Top Categories** and **Top Tags** — were added below the position-category / sleeper-hits grid. Both are computed client-side from `M.content_drill_table` after applying the current filter scope (Position Cat / Position / Author / Category / Tag / Title search): the row set is iterated, `categories` / `tags` strings are split on commas, and `unique_clicks` + `total_clicks` are summed per label. The top 10 labels by unique clicks are shown with paired bars (unique vs total) and re-render on every filter change via `_crFilter()` → `_crRenderTopBar()`.
25. **Overview KPI ordering + Recent Campaigns Metrics table**: Overview KPI cards are split into two rows. Primary row (most important): Active (Send-to), Avg Open Rate, Avg Click Rate, Campaigns Sent. Secondary row: Total Subscribers, Unsubscribed, Bounced, Quiz Takers. The bottom "Top Campaign Open Rates" table was renamed to **Recent Campaigns Metrics** and now lists the **last 10 sent campaigns by date** (most recent first), sorted client-side on `sent_date` DESC from `M.campaign_table` instead of the top-by-open-rate set.
26. **Sunday Spotlight toggle default OFF**: The Campaigns tab toggle ships unchecked, so Sunday Spotlight is excluded by default. Initial label state is "Excluding branded digest sends".
27. **Sent Date column nowrap**: A `.nowrap` utility class (`white-space: nowrap`) is applied to the Sent Date `<td>` cells in both the Campaigns paginated table and the Overview Recent Campaigns Metrics table so the date stays on one line even on narrow columns.
28. **Header restyle (pill + iOS dark switch)**: Top header on the right now shows a `Daily updates` pill (blue accent) followed by the `data_as_of` date (large), with a `SuperAge — Brand Pulse · Last updated` muted subtitle below. The dark-mode control is an iOS-style switch with a `DARK` label, replacing the old `Dark mode` button. State persists in `localStorage['sa-dark']` and is synced back to the checkbox on load via a small init block in the `DARK MODE` script section.
29. **Click Analysis filter + per-campaign default + completed-period comparison**: Click Analysis tab gets an "Include Sunday Spotlight" toggle (default OFF) plus a Section 1 metric dropdown (Total clicks / Clicks per campaign / Click-to-Open %) defaulting to **Clicks per campaign** so an uneven send count between months doesn't distort the visual. Section 1 charts re-aggregate from `M.campaign_table` client-side so the toggle and metric work without a lambda re-run. Section 2 (raw click events) reads new `clicks_no_ss` arrays added to `raw_clicks_same_weekday`, `raw_clicks_by_weekday`, `raw_clicks_weekly`, and `raw_clicks_monthly` — each computed via `COUNT(*) FILTER (WHERE issue_name NOT ILIKE '%sunday spotlight%')` in `superage_comparison_lambda.py`. WoW / MoM insight text now compares the last two **completed** periods (`is_current[i]` skipped) so an in-progress week/month no longer triggers a misleading drop percentage.
30. **Click Analysis cleanup — duplicates removed**: The "Clicks by Category", "Clicks by Author", and "Tag / Topic Performance" blocks were removed from the Click Analysis tab; the same breakdowns already live on the Content Reference tab (Top Categories / Top Tags charts + Author filter + Sleeper Hits insight). The duplicate per-category / per-author / per-tag aggregation queries (`article_category_clicks`, `article_writer_clicks`, `article_tag_clicks`) were retired from the lambda in the same cleanup pass — see note 36.
31. **Top Articles simplified to top-10 all-time**: The All / 7d / 15d / 30d / 90d window selector on the "Top Articles by Unique Clicks" table was removed (only "All" had data). The table now renders the **top 10** rows of `M.top_articles` (sorted by `unique_clicks` DESC in Q13). The `top_articles_windowed` JSON field and its underlying 4× per-window SQL block were retired from the lambda.
32. **Same Weekday switched to campaign-send view**: The "Same Weekday" chart in Click Analysis Section 2 was rewritten to bucket **campaign sends** (from `M.campaign_table`) by the weekday of `sent_date`, then plot each campaign's **total clicks** as a bar (replacing the click-event-date view sourced from `raw_clicks_by_weekday`). For each weekday it shows the last 2 / 3 / 5 campaigns (window buttons re-labelled "2 weeks / 3 weeks / 5 weeks"). Each bar is colour-coded **green** if total clicks ≥ that weekday's average and **red** if below; campaigns sent within the last 2 days render in pale grey ("in progress") and are excluded from the average so their incomplete totals don't drag the baseline. The tooltip surfaces the campaign name, send date, total clicks, and the ± % vs that weekday's average. The Sunday Spotlight toggle still applies (matched by `name`). `raw_clicks_by_weekday` / `raw_clicks_same_weekday` are no longer consumed by this chart but stay in the JSON for backwards-compat.
33. **Position Category bars switched to avg-per-article**: "Unique Clicks by Position Category" renamed to "Clicks by Position Category" and its bars now plot **avg unique** / **avg total** clicks per article for each High / Medium / Low bucket (1 d.p.). Article count drawn above each green bar via an inline plugin; tooltip lists the absolute totals + article count.
34. **Top Categories / Top Tags bars switched to avg-per-article**: bars now plot `Avg Unique Clicks / Article` and `Avg Total Clicks / Article` (integer round); top 10 re-sorted by avg unique DESC. The inline label at each bar's right edge switched from `avg N/article` to `N articles` so the volume backing each average is visible.
35. **Acquisition Quality by Source — time window + churn columns + naming**: Audience tab table renamed (UTM → Source) and given a time-window selector (All / 30d / 60d / 90d). The lambda runs Q19 four times (one per window) and ships `rows_all`, `rows_30d`, `rows_60d`, `rows_90d` inside `M.acquisition_quality.utm_source`. Click stats are now sourced from raw `Campaigns_Clicks` joined to `subscribers.email`, so the date filter scopes click activity. New columns: 30-Day Churn % and 90-Day Churn % (cohort-style — % of source cohort who unsubscribed within N days of joining). Two placeholder columns are reserved: Avg Sponsor Click / Subscriber (renders `—`, waiting on a `Campaigns_Clicks` ↔ `articles_clicks` join key — affiliate not separately tracked) and Open Rate % (renders `—`, no per-subscriber open data yet). CAC is intentionally skipped (no spend data in the schema).
36. **Dead-query sweep (2026-05)**: All `cur.execute(...)` blocks whose output the dashboard never consumed were removed from `superage_metrics_lambda_updated.py`. Retired queries / JSON fields (with their old query numbers): Q3-old `subscriber_growth`, Q5 `sub_level_dist`, Q6 `source_dist`, Q7 `us_based_count`, Q10 `top_campaigns`, Q12 `content_type` / `content_type_table`, Q14 `article_category_counts`, Q15-legacy `article_category_clicks`, Q16-legacy `article_tag_clicks` + `article_writer_clicks`, Q18-bucketed `click_distribution` (only `total_clickers` kept), Q41-style `cohort_utm_retention`, plus `top_articles_windowed`, `article_click_comparison`, `total_all_states`, `active_subscribers`, `deleted_count`, `quiz_count`, `avg_age_quiz`, and the four `total_*_camp` campaign-aggregate dead totals. Lambda now ships only fields the dashboard reads. The renamed sections (Longevity Quiz → Audience Persona, UTM Source → Source, etc.) are reflected in the Q21–Q29 section titles and the Q19 / Q20 docs.
37. **Longevity Quiz tab renamed to Audience Persona**: tab button, section banner, and JS comment all switched to "Audience Persona". The Avg / Max Longevity Score KPI cards and the Longevity Score Distribution chart were removed (the data wasn't actionable for personas); Q24 SQL + `quiz_score_dist` JSON were retired in the same pass. Q21 (Quiz KPIs) was trimmed to `COUNT(*)` + `AVG(age)` — `avg_score` / `min_score` / `max_score` columns dropped. The "Longevity Quiz" name now only survives in user-facing copy where it accurately describes the **data source** (e.g. "Total Quiz Takers — Longevity quiz completions"); the **tab name** is "Audience Persona".
38. **Monthly Revenue chart restyled to match Monthly Campaign Clicks**: the Revenue tab's headline chart was changed from a red-future-bars + dashed-line-split + "Future →" separator layout to a single solid bar series styled like the Monthly Campaign Clicks chart on the Click Analysis tab. Bars are green (`#1a7f37`); current and any not-yet-closed months render at 40% alpha with a 2 px solid border (same `color + '66'` pattern used by `_clickTrendChart`). The dashed vertical separator and the "Future →" plugin label were removed. The deals line is now a single continuous overlay (no past/future split, no second "Deals (future)" dataset). Insight text under the chart reads: *"Bars = revenue (left axis). Line = number of deals (right axis). Faded bars are the current (in-progress) or not-yet-closed months."*
39. **Click Analysis: missing `.h240` CSS rule added**: the raw Weekly / Monthly cards on the Click Analysis tab used `class="chart-container h240"` but `.chart-container.h240` had no `height` rule in the style block (the CSS only defined `h220 / h260 / h300 / h340 / h380 / h420`). That collapsed those canvases to 0 px tall — the headers, range buttons and insight strings rendered but the bars were invisible. Added `.chart-container.h240 { height: 240px; }` to match the rest of the size scale.
40. **Top Tags "Tag appears in ≥ N articles" filter (2026-05)**: a five-button group (`1 / 3 / 5 / 7 / 10`) was added above the Top Tags chart on the Content Reference tab. The filter drops tags whose article count is below the threshold **before** the top-10 ranking is computed by avg unique clicks per article, so single-article tags can't dominate the ranking. The threshold is stored in `window._crTagMin` (default `1`, no filter) and is layered on top of the existing scope filters (Position Cat / Author / Category / Tag / Title search). `_crSetTagMin(n)` is the toolbar handler; it re-renders only the Tags chart — Top Categories is intentionally **not** filtered the same way. Insight text below the chart appends *"(showing tags that appear in ≥ N articles)"* when N > 1; if the filter empties the set, the insight switches to *"No tag in scope appears in ≥ N articles"*. Threshold resets to `1` on page reload.
41. **Audience tab: unified time-window selector (2026-05)**: a single `All time / Last 30 / 60 / 90 days` toggle was lifted out of the Acquisition Quality table header and placed at the top of the Audience tab. The toggle now drives every component on the tab in one render — the four KPI cards (Total Subscribers flips to "New Subscribers" + "Joined in last N days" when filtered; Top Source + Top Source % recompute from the windowed cohort), the Acquisition by Source doughnut, the Source Clicks Performance bar chart, and the Acquisition Quality table — via `_setAcqWindow(win)`. The pie / table read `acquisition_quality.utm_source.rows_<win>` (Q19). The bar chart uses `utm_clicks_performance` (Q20, with the proper unique-vs-total split) for the all-time view, and falls back to Q19 rows for the windowed views (where unique == total because Q19 counts raw `Campaigns_Clicks` events). The Active Rate KPI stays global and is labelled "Currently active (global)" so it's clear it isn't window-scoped. State isn't persisted to localStorage.
42. **Source-label canonicalisation moved server-side (2026-05)**: `utmLabel()` in `index.html` is now mirrored by an SQL helper `_canon_source(col)` in `superage_metrics_lambda_updated.py` that runs **before** the `GROUP BY` in Q19 / Q20 / Q35b, collapsing aliases (raw `meta` / `facebook` / `fb` / `ig` → one `Meta` row; `taboola`/`Taboola`/`TABOOLA` → one `Taboola`, etc.) in the rollup rather than at display time. The duplicate display rows that used to appear when the rollup was on the raw `utm_source` value (most visibly the "two Meta rows" issue) are gone. **Full mapping table is documented in §4 → "Source label canonicalisation (source of truth)" above** — that table is the single source of truth; when adding a new branch update both `_canon_source` and `utmLabel` in lockstep.
43. **Source-canonicalisation expansion (2026-05, follow-ups)**: the canonicaliser was extended in three passes after the initial move-to-SQL — (a) split `IFCPL` (`if`/`ifcpl1`) out of `Meta` into its own bucket; (b) absorbed every TrueDemocracy CPL2 batch via `LIKE 'td_cpl2%'`, every TheAgeist sample/request issue via `LIKE 'ageist_%'`/`LIKE 'ageistrequest%'`, and every RecommendedReads CPL1 suffix via `LIKE 'rrcpl1%'` so the brand families don't fragment by date or A/B suffix; (c) added new top-level labels — `NNCPL` (NNCPL1 + NN_CPL2_* + NN1_CPL2oneclick), `ISCPL` (IS + ISCPL1), `AI` (chatgpt.com + perplexity + nbot.ai), plus standalone `Refind` and `SuperAge` (kept distinct from `SuperAge Quiz`). The matching `CASE` in Q35b was updated alongside (a) so the Retention-by-Acquisition-Source buckets stay consistent — its insight string in `renderRetention()` now lists `Meta (facebook/fb/ig/meta), IFCPL (if/ifcpl1), Google, Direct`.
44. **Survival curve overlay per acquisition source (2026-05)**: the Retention tab's Survival Curve was upgraded from a single all-subscriber line into an overlay chart — the all-subscriber baseline (filled green, Q37) plus one thin line per source bucket from the new Q37b. Source buckets use the canonical mapping from §4 ("Source label canonicalisation"), with `'organic' / 'direct' / ''` collapsed into a **Direct** bucket. Top 8 sources by cohort size; minimum cohort 500 subscribers to keep noisy tail sources out of the chart. Tooltip uses `mode:'index'` so hovering a day shows every series' retention at once; legend is at the bottom and each entry includes the cohort size. Q35b was retrofitted in the same pass to use the canonical mapping instead of its previous hand-coded six-bucket list — the Retention-by-Acquisition-Source table now shows the same source labels as the Audience tab. JSON shape: `M.survival_curve_by_source = { labels: [...], series: [{label, total, rates}] }`.
45. **Overview donut switched to "Current Subscriber Engagement Mix" (2026-05)**: the four-state CreateSend donut (Active / Unsubscribed / Bounced / Deleted) was replaced with a three-slice engagement view computed off the **current list only** — i.e. subscribers we still hold (state='Active' or 'Bounced'); Unsubscribed and Deleted are excluded because those records have left the list and were diluting the engagement picture. New JSON field `M.subscriber_engagement_mix = {labels:["Send-To","Dormant / Ghost / Zombie","Bounced"], data:[…], colors:[…]}` is emitted by the lambda from existing variables (no extra SQL — derived from `send_to_active`, `total_subscribers`, `bounced_count`). The Overview chart now reads the new field and falls back to the legacy `subscriber_states` shape only if the lambda hasn't re-run yet. Insight strip rewritten to describe Send-To / Dormant / Bounced rather than the four CreateSend states. Q2 in METRICS_updated.md and the Section 1 KPI table were rewritten in lockstep.
46. **Survival Curve — multi-select "Filter sources" dropdown (2026-05)**: the per-source overlay (Q37b) was opened up from "top 8 by cohort" to "every canonical bucket with ≥ 100 subscribers" — no LIMIT — and the toggle UI was rebuilt as a **multi-select dropdown panel** instead of relying on Chart.js's built-in legend. Chart.js's legend is now disabled (`plugins.legend.display = false`); a "Filter sources" button above the chart opens a checkbox panel populated dynamically with one entry per per-source dataset (colour swatch + canonical label) plus All / None bulk buttons at the top. Each checkbox calls `_retCurveToggleSrc(idx, visible)` to flip a single dataset; the All / None routes through `_retCurveAll(visible)` which also syncs the checkbox states. The button's summary text ("— all N selected", "— 4 of 12 selected", "— none selected (baseline only)") updates via `_retCurveUpdateSummary()`. The menu closes on outside-click. The "All subscribers" baseline (dataset 0) is always visible and isn't represented in the dropdown so the reference line can't be hidden accidentally. Palette expanded from 8 to 16 distinct colours to cover the longer source list.
49. **LTV renamed to Lifespan (2026-05)**: the per-source Retention table column headers "Avg LTV (days)" / "Median LTV" were renamed to "Avg Lifespan (days)" / "Median Lifespan" (the underlying field names `avg_lifespan_days` / `median_lifespan_days` stay the same — the formula was never revenue-based, just `date_unsubscribed - date_joined` in days across churned subscribers). Banner copy + insight string + Section 7 KPI table updated; the calculation in Q35 / Q35b is unchanged.
47. **Revenue & Sponsors: ECPM retired (2026-05)**: the "Avg ECPM" KPI card on the Revenue & Sponsors tab and the "Avg ECPM" column on the Top Sponsors table were removed. The dedicated `AVG(NULLIF(ecpm,'')::numeric)` SQL block was deleted from the lambda, along with the `AVG(NULLIF(ecpm,'')::numeric)` aggregate on the per-sponsor query. `M.avg_ecpm` no longer ships in the JSON, and `top_sponsors[].avg_ecpm` is gone. Q34 marked retired; Section 6 of METRICS_updated.md updated. Future-dated rows in `sa_airtable_sales` are intentionally **kept** — the Monthly Revenue chart shows them as faded bars (the `isInProgress(label)` helper in `index.html` flags any month ≥ current month and reduces its bar alpha), so booked-but-not-yet-run placements stay visible alongside historical revenue.
48. **Cohort Performance Table reshaped — 2025+ only with three explicit cohort counts (2026-05)**: Q40 was restricted to cohorts with `date_joined >= '2025-01-01'` so the table focuses on recent acquisition quality (the heatmap above keeps its longer history). Columns rebuilt around three primary counts derived from the same scan: **Cohort Size** (subscribers who joined the month — `COUNT(*)`), **Total Subscribers** (still on the list today — `COUNT(*) FILTER (WHERE state IN ('Active','Bounced'))`), and **Total Active** (state='Active' AND engagement_segment NOT IN Ghosts/Zombies/Dormant). Derived rates: **Still on List %**, **Retention %**, **Churn %**, **Early Churn (90d)**. Existing **Campaigns Sent** column kept for context. Lambda now emits `cohort_table[].total_subscribers` + `cohort_table[].still_on_list_pct`; the dashboard table renders the new shape and tooltips document each column's formula.
50. **Overview donut → engagement-segment split within state='Active' (2026-05)**: the Current Subscriber Mix donut was rescoped again — denominator is now **current subscribers only** (`state = 'Active'`) rather than every row in `subscribers`. Five slices now show how that active base breaks down by engagement segment: **Send-To** (the canonical reachable bucket — engagement_segment NOT IN Ghosts/Zombies/Dormant), **Zombies**, **Ghosts**, **Dormant**, **Other** (residual — null / empty / unrecognised segment). The SQL gained per-segment FILTER counts inside the existing Active-only query; the lambda still emits the unified `M.subscriber_engagement_mix = {labels, data, colors}` shape so the dashboard chart didn't need restructuring. Unsubscribed / Bounced / Deleted no longer appear in this donut — they're tracked elsewhere (subscriber-status KPIs, retention table). Banner copy + Q2 + Section 1 KPI bullet rewritten in lockstep.
51. **Revenue tab: sponsor_type filter + Line Amount relabel (2026-05)**: every query against `sa_airtable_sales` (totals KPIs, Monthly Line Amount chart, Top Sponsors aggregation, Sponsor Type donut) now requires `sponsor_type IS NOT NULL AND TRIM(sponsor_type) != ''` — rows missing a sponsor type previously inflated totals and contributed a phantom "Unknown" slice in the donut, both gone now. In the same pass the user-facing wording on the tab was changed from "Revenue" → "Line Amount" to match the underlying Airtable column name (`$_line_amount`): KPI card "Total Revenue" → "Total Line Amount", chart heading "Monthly Revenue & Deal Volume" → "Monthly Line Amount & Deal Volume", Top Sponsors columns "Revenue" + "Avg Rev / Deal" + "Revenue Share" → "Line Amount" + "Avg Line / Deal" + "Share of Total". JSON field names (`total_revenue`, `total_revenue_fmt`, `top_sponsors[].revenue`) stay untouched for backwards-compat.
52. **Cohort table: "Still on List %" + "Retention %" → single "Active %" column (2026-05)**: the Cohort Performance Table previously carried two near-duplicate percentage columns — Still on List % (`total_subscribers / cohort_size`, where total_subscribers = state IN Active+Bounced) and Retention % (`active_now / cohort_size`). Consolidated into one column called **Active %** with formula `total_active / cohort_size` (canonical Active rule: state='Active' AND engagement_segment NOT IN Ghosts/Zombies/Dormant). The lambda now emits `cohort_table[].active_pct`; the HTML reads that field and falls back to the legacy `retention_pct` if the lambda hasn't re-run yet. The deprecated `still_on_list_pct` JSON field is no longer emitted.
53. **Click Analysis: Sunday Spotlight toggle now drives the KPIs + moved above the cards (2026-05)**: the Click Analysis toolbar (Include Sunday Spotlight switch + Section 1 metric dropdown) was lifted to the top of the tab — sits **above** the four KPI cards now — and the KPIs themselves were rewired to react to it. The card set was rebuilt around campaign-level aggregates that can be filtered by name: **Campaigns Sent**, **Total Recipients**, **Total Clicks**, **Avg Clicks / Campaign**, all summed from `M.campaign_table` rows filtered with the same `name NOT ILIKE '%sunday spotlight%'` predicate the chart re-aggregators use. `_renderClickKpis()` (new) runs in `_refreshClicksTab()` and on initial render so the numbers stay in sync with the bars below. The legacy article-level KPI fields (`total_article_clicks` / `unique_article_clickers` / `articles_clicked_count` / `avg_clicks_per_article`) couldn't be filtered by Sunday Spotlight at the SQL layer (no campaign name on `articles_clicks`), so they're no longer surfaced on this tab; the JSON fields still ship as backwards-compat for any external consumer.
54. **Overview donut: merge Dormant / Ghost / Zombie into one slice (2026-05)**: the Current Subscriber Mix donut on the Overview tab was reduced from five slices to **three** — the three disengaged segments (`Zombies`, `Ghosts`, `Dormant`) now sum into a single **Dormant / Ghost / Zombie** bucket. The SQL still produces per-segment FILTER counts (kept for future reuse), but `M.subscriber_engagement_mix.data` ships `[send_to, zombies + ghosts + dormant, other_residual]` and the chart's three slices are coloured green / amber / grey. Insight strip + Q2 docs + Section 1 KPI bullet updated to match.
55. **Monthly Churn Volume: add churn % of sends line (2026-05)**: Q38 was extended to also compute `total_sent` (sum of `Recipients` for campaigns with `Recipients > 95` sent that month) and `churn_pct = churned / total_sent * 100`. The Retention tab's Monthly Churn chart switched from a single red bar series to a mixed bar + line — red bars stay on the left axis (unsubscribe volume), new blue line on the right axis (churn % of sends, with 3-decimal-place ticks). Months with no qualifying send (`Recipients > 95`) show a bar but no line point so the rate axis doesn't spike on divide-by-zero. JSON `M.monthly_churn` now also carries `total_sent`, `campaigns`, `churn_pct` arrays alongside the legacy `data` array. The signal: a falling churn-% line while send volume bars stay flat or grow is the "healthy" pattern — more reach without proportional damage to the list. **Caveat:** `Recipients` is per-send and not deduplicated across campaigns, so the denominator is total send events / impressions (a subscriber receiving 4 emails contributes 4× to `total_sent`). That's intentional — it gives a per-impression damage signal rather than per-unique-person; documented on the chart insight strip and in Q38.
56. **Cohort table: relabel "Total Active" → "Active (Send-To)" + "Active %" → "Active % (of Cohort Size)" (2026-05)**: column headers + their `title=` tooltips updated on the Cohort Performance Table to match the canonical Send-To terminology used elsewhere on the dashboard. Underlying JSON field names (`active_now`, `active_pct`) unchanged.
57. **Weekly Digest tab + Q_WD (2026-05)**: a new **Weekly Digest** tab was added between Overview and Campaigns. It shows six headline KPI tiles for the **last completed Mon–Sun ISO week** (compared against the **prior completed Mon–Sun**) plus a Top Acquisition Source readout. Tiles: Net New Subscribers, Send-To Base (end-of-week from `growth_history.total_active`), Churn % of Sends (reuses the same formula as Q38 — unsubs / total_sent × 100), Campaigns Sent (count only, no recipients), Avg Open Rate, Avg Click Rate. Each tile renders the current value, the WoW delta (% change with up/down arrow, coloured green/red where direction is unambiguously good or bad), and a 48-px sparkline of the 8 prior completed weeks (the in-progress current week is excluded from both the delta math and the sparkline so partial-week data can't distort the read). No flags, no PDF/email export — basic version per the user request.

    **Architecture**: the aggregation lives in `superage_comparison_lambda.py` (not the metrics lambda) so it refreshes on the comparison cadence (every 6 hours). A new SQL block joins four CTEs — `weeks` (generate_series of 9 Mon–Sun weeks), `joins` / `unsubs` (subscriber-level counts), `camps` (per-week campaign aggregates with `Recipients > 95`), and `gh` (`growth_history.total_active` MAX per week) — into one row per week. A second query computes per-week top-source counts using a duplicated canonical-source CASE (mirroring `_canon_source` in the metrics lambda + `utmLabel` in `index.html` — three sources of truth, kept in sync manually). JSON ships as `M.weekly_digest = {labels, week_starts, is_current, new_subs, unsubs, campaigns_sent, total_sent, avg_open_rate, avg_click_rate, churn_pct_of_sends, active_eow, top_sources_by_week}`.

    **Same `Recipients` dedup caveat** as Q38 applies: `total_sent` and the churn-% denominator count send impressions (one subscriber receiving N emails contributes N), not unique people reached — intentional for per-impression rate signals.
58. **Weekly Digest: Slack-style summary block + article-level breakdowns (2026-05)**: the basic Weekly Digest tab now carries a second card below the tiles that mirrors the `#weekly_automation` Slack post format — an Overall line ("N campaigns · X sent · Y% open rate (delta pp) · Z% click rate (delta pp) · W% unsub (delta pp)"), a New Subscribers breakdown by canonical source ("This week we had N new subscribers (A SourceA [%], B SourceB [%], …)"), and three top-5 lists pulled from `articles_clicks` for the last completed Mon–Sun week, split by `type`: **Top Content Hits** (type NOT IN sponsor/immersion/games/waitlist), **Sponsor Clicks** (`type='sponsor'`), and **Immersion Clicks** (`type='immersion'`). A new SQL block in the comparison lambda returns one row per (type, article_title, url, issue_name, issue_date) for that week (LIMIT 200, ORDER BY unique_clicks DESC); Python then splits into three lists and ships them on `M.weekly_digest.top_content_this_week`, `top_sponsors_this_week`, `top_immersions_this_week`. **The "Net New Subscribers" tile was renamed to "New Subscribers"** because the count is gross (every `date_joined` row in the week regardless of subsequent unsubs), not the net-of-churn the old label implied. The numbers themselves didn't change — only the label.
59. **Weekly Digest tweak: Net Change tile + editorial/sponsor/immersion lists only (2026-05)**: the "New Subscribers" tile was replaced with **Net Change** = `new_subs − unsubs` per week (computed client-side from the existing JSON arrays — no new lambda field needed). Sparkline + WoW delta both follow the netted series; values prefix `+` when positive so the sign reads at a glance. Article-level lists in the summary block were tightened to the three placement types the user actually wants — **Editorial Clicks** (`type='editorial'`), **Sponsor Clicks** (`type='sponsor'`), **Immersion Clicks** (`type='immersion'`); the previous catch-all "Top Content Hits" (which silently absorbed any non-sponsor/non-immersion type, including null) is gone. SQL filter in the comparison lambda changed from `LOWER(type) NOT IN ('games','waitlist')` to `LOWER(TRIM(type)) IN ('editorial','sponsor','immersion')`; JSON key renamed `top_content_this_week` → `top_editorial_this_week`. HTML reads the new key with a backwards-compat fallback to the old one until the lambda re-runs.
60. **Content Reference: All Articles gains an "Issue" column (2026-05)**: Q16b's SELECT now also pulls `articles_clicks.issue_name` + `issue_date`, and the lambda emits `content_drill_table[].issue_name` + `content_drill_table[].issue_date` (YYYY-MM-DD slice). The HTML table on the Content Reference tab gained an Issue column between Title and Category — value is `<issue_name> (<issue_date>)` with a muted date subscript, falling back to an em-dash when the lambda hasn't been re-run yet. No new SQL block — just additions to the existing inner-join. Reminder: each `articles_clicks` row is one (article, issue) placement, so an article featured in N sends appears N times in this table (one per send) — the new Issue column makes which-send-this-row-belongs-to explicit.
61. **Monthly Churn split into two visuals (2026-05)**: Q38 was extended with a second rate variant — **`campaign_unsub_pct`** = `SUM(Campaigns.Unsubscribed) / SUM(Campaigns.Recipients) * 100` per month (both columns from `Campaigns`, same `Recipients > 95` window). The Retention tab now shows two separate charts: **Monthly Churn Volume** (bars only — the per-send rate line that used to overlay it was removed) and a new **Campaign Unsubscribe Rate** card with the pure-Campaigns rate plotted as its own filled line. JSON `M.monthly_churn` now ships `campaign_unsubs` and `campaign_unsub_pct` arrays alongside the existing `data` / `total_sent` / `churn_pct`. The list-churn-per-send formula (`subscribers.date_unsubscribed / Campaigns.Recipients`) stays in `churn_pct` for anything that still wants it — the Weekly Digest's "Churn % of Sends" tile is unchanged.
62. **Weekly Digest: drop Send-To Base tile + visualise the summary (2026-05)**: the **Send-To Base (end of week)** KPI tile was removed (`growth_history.total_active` field stays in the JSON so other consumers don't break — the tile just doesn't render). The text-based summary block was replaced with charts: the source breakdown line is now a doughnut (`New Subscribers by Source`, slices = canonical source bucket counts), and the three article lists (Editorial / Sponsor / Immersion) are now horizontal bar charts with paired Unique / Total bars per article, click-through to URL on bar click, full title + issue name + send date in the tooltip. Overall stats strip + the Recipients-not-deduplicated caveat stay as text. `_digestSummaryBody()` now returns markup with canvas placeholders; `_initDigestCharts()` runs after the markup is injected to instantiate the four charts.
63. **Weekly Digest: tighter campaign filter + pp-only deltas (2026-05)**: the campaign aggregates that feed the digest's Overall stats and KPI tiles (Q_WD `camps` CTE) now require **`Recipients >= 200,000`**, up from the dashboard-wide `Recipients > 95` threshold. Rationale: the digest is meant to summarise mass-send performance; small dedicated / segmented campaigns (newsletters to ~10–20 K, A/B test pilots, etc.) have atypical open and click rates that disproportionately move a simple `AVG(UOpenRate)` over the week. The `200,000` cutoff isolates the mass-send cohort. Other dashboard surfaces (Campaigns tab, Retention tab Q38 churn rate, Cohort table campaigns-sent column) keep the `> 95` filter — only the Weekly Digest tightens.

    In the same pass, **all KPI tile deltas switched to absolute differences** — rate tiles (Avg Open Rate / Avg Click Rate / Churn % of Sends) now render `▲/▼ X.XX pp` (percentage points), count tiles (Net Change / Campaigns Sent) render `▲/▼ N` (plain signed integer). The relative-percent change variant was removed because it overstates moves on small bases (a click rate going `0.5% → 1%` is `+100%` relative but only `+0.5 pp` — the pp number is the honest one). The Overall stats card was already using pp; the tiles + summary now agree.

    Banner copy + Overall card insight strip surface the `Recipients ≥ 200,000` rule so the filter isn't a hidden convention.
64. **Survival Curve overlay: sort sources by 365-day retention (2026-05)**: Q37b's `ORDER BY total DESC` was replaced with `ORDER BY (alive_365 / total) DESC NULLS LAST, total DESC`. The "Filter sources" dropdown on the Retention tab now lists buckets best-retention-first instead of biggest-cohort-first — the source whose survival line stays highest at the year mark sits at the top. Cohort size remains the tie-breaker so a large 65%-retention bucket surfaces over a tiny 65%-retention bucket. Affects only the row order in the dropdown + the dataset order on the chart (which drives palette colour assignment and z-order); the underlying retention numbers don't change.
65. **Overview Campaign Performance Trend — one metric per chart, X axis = date, Sunday Spotlight toggle (2026-05)**: the dual-line "Campaign Performance Trend" chart on Overview (which used Open Rate + Click Rate on a shared y-axis with truncated campaign names on the x-axis) was rebuilt as two separate single-metric charts laid out side-by-side: **Open Rate over time** + **Click Rate over time**, each plotting one variable so the trend is unambiguous (one chart, one primary feature). The x-axis on both is now the campaign's **send date** (YYYY-MM-DD), sorted chronologically — one point per qualifying campaign — instead of truncated campaign names. A **Sunday Spotlight include/exclude toggle** sits above the charts (default OFF = excluded, matching the Click Analysis toggle convention) — branded-digest sends typically have atypically high opens and were the visible cause of the "spiky" appearance. Charts are rendered client-side from `M.campaign_table` so the toggle flips without a lambda re-run. Click any point to open that campaign's CreateSend URL in a new tab. The lambda-time `M.campaign_trend` payload is no longer consumed by Overview — kept in the JSON for backwards-compat but ready to retire.
66. **Overview Subscriber Growth chart: latest-snapshot semantics + daily/weekly/monthly granularity filter (2026-05)**: the `total_active` column on the growth-history chart used to be `MAX(total_active)` per month — i.e. the **peak** active count during the month, not its end-state value. That created a visible ~1.5K gap between the chart's last point and the lambda's real-time `M.total_subscribers` whenever the monthly peak fell mid-month. The aggregation is now **"latest snapshot per bucket"**: a `ROW_NUMBER() OVER (PARTITION BY DATE_TRUNC(unit, snapshot_date) ORDER BY snapshot_date DESC)` selects the most recent snapshot's `total_active` per bucket, while `gained` / `lost` still SUM across the bucket. The chart endpoint now matches the lambda's live count up to (typically) <1 day of churn.

    In the same change, **three granularities are emitted**: `daily` (last 120 days), `weekly` (last 52 weeks), `monthly` (last 36 months). Lambda emits `M.subscriber_growth_series = { daily, weekly, monthly }` where each carries `labels` + `new_subs` + `unsubs` + `active_count`. `M.subscriber_monthly` stays populated from the monthly bucket for backwards-compat.

    Overview UI adds a **Granularity selector** (Day / Week / Month) plus a **dynamic Window button row** whose presets match the active granularity — Day shows `All / 90d / 30d / 14d / 7d`, Week shows `All / 26w / 12w / 8w / 4w`, Month shows `All / 24m / 12m / 6m / 3m`. Both selectors re-slice the cached payload client-side (no lambda re-run). Defaults: granularity = Month, window = 12.
67. **subscriber_acquisition table integration (2026-05)**: a new `superage.subscriber_acquisition` table provides acquisition-source attribution more accurate than the legacy `subscribers.utm_source` / `source` columns. Schema: `email`, `acquisition_date`, `acquisition_utm_source`, `acquisition_status`, `brand`, `dynamo_id`, `message`, `acquisition_sub_level`, `acquisition_sub_source`, `acquisition_o_event`, `ingested_at`. Integration adds a LEFT JOIN in Q19 / Q20 / Q35b / Q37b (metrics lambda) and the top-source query (comparison lambda) on `LOWER(TRIM(email))` with filter `acquisition_status IN ('added', 'resubscribed')`. **Label priority**: `sa.acquisition_utm_source` >> `s.utm_source` >> `s.source` >> `'Organic'`. **Effective join date**: `COALESCE(sa.acquisition_date, s.date_joined)` — when SA has a non-NULL `acquisition_date` it replaces `date_joined` for lifespan and churn-window calculations. **Taboola exclusion**: all source-grouped result sets add `WHERE bucket != 'Taboola'` (or equivalent). `utmLabel()` in `index.html` requires no changes.
