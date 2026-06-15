# campaigns_life.json — Query Reference

All queries executed by the Lambda that refreshes `campaigns_life.json`.  
URL pattern variables (`:PATS`) default to:

```
%games.superage.com%
%o.superage.com/r?dest=games.superage.com%
```

---

## Derived Metric Definitions

| Metric | Formula | Notes |
|---|---|---|
| **avg_open_rate** | `SUM(unique_opens) / SUM(recipients)` | Uses unique opens, not total opens |
| **game_ctr** (KPI) | `total_unique_game_clickers / SUM(unique_opens)` | Cross-campaign deduped clickers / total unique opens |
| **game_ctr** (per campaign) | `campaign_unique_game_clickers / campaign_unique_opens` | Per-campaign deduped clickers / that campaign's unique opens |
| **Total Opens KPI** | SA: `SUM("TotalOpened")` · AH: `SUM(opens)` · Ageist: `SUM(total_opens)` | All three brands use their non-unique total opens column for display |
| **Funnel pcts** | Each step / recipients × 100 | Recipients is always the 100% base |

---

## SuperAge

Schema: `superage` (configurable via `SA_SCHEMA`).  
All patterns applied against `"Campaigns_Clicks"."URL"`.

---

### 1. Identify game campaigns

```sql
SELECT DISTINCT issue_name
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(:PATS)
  AND issue_name IS NOT NULL;
```

**Output:** List of `issue_name` values that contain at least one games-URL click.  
**Used for:** Filtering all downstream SA queries to game-linked campaigns only.

---

### 2. Per-campaign game clicks & unique game clickers

```sql
SELECT issue_name,
       COUNT(*)                        AS clicks,
       COUNT(DISTINCT "EmailAddress ") AS unique_clicks
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(:PATS)
  AND issue_name IS NOT NULL
GROUP BY issue_name;
```

**Output:** `clicks` = total game URL clicks per campaign; `unique_clicks` = distinct email addresses that clicked.  
**Used for:** `game_clicks`, `game_unique` per campaign row.

---

### 3. Campaign metadata

```sql
SELECT "Campaign Name",
       "Sent Date ",
       "Subject",
       "URL",
       "Recipients",
       "UniqueOpened",
       "Clicks",
       "Unsubscribed",
       "UOpenRate",
       "UClickRate"
FROM superage."Campaigns"
WHERE "Campaign Name" = ANY(:life_issues)
ORDER BY "Sent Date " DESC NULLS LAST;
```

**Output:** One row per campaign with send-level aggregates.  
**Used for:** `recipients`, `unique_opens`, `open_rate`, `click_rate`, `unsubs` per campaign.

---

### 4. Funnel totals

```sql
SELECT COALESCE(SUM("Recipients"),   0) AS recipients,
       COALESCE(SUM("UniqueOpened"), 0) AS unique_opens,
       COALESCE(SUM("Clicks"),       0) AS total_clicks,
       COALESCE(SUM("Unsubscribed"), 0) AS unsubs
FROM superage."Campaigns"
WHERE "Campaign Name" = ANY(:life_issues);
```

**Used for:** KPI cards and the funnel chart totals.

---

### 5. Total game clicks & unique game clickers

```sql
SELECT COUNT(*)                        AS total_clicks,
       COUNT(DISTINCT "EmailAddress ") AS unique_clicks
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(:PATS);
```

**Output:** Aggregate across all SA sends.  
**Derived:** `game_ctr = unique_clicks / unique_opens` (from query 4).

---

### 6. Monthly game click trend

```sql
SELECT DATE_TRUNC('month', "Date")::date AS month,
       COUNT(*)                           AS clicks,
       COUNT(DISTINCT "EmailAddress ")    AS unique_clicks
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(:PATS)
  AND "Date" IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

**Used for:** `monthly_trend.clicks`, `monthly_trend.unique_clicks`.

---

---

## AllHealthy

Schema: `public`.  
Game clicks sourced from `campaign_top_links`.  
Unique game clickers sourced from `allhealthy_contact_clicks` (primary) or `campaign_top_links` (fallback).

---

### 1. Identify game campaigns

```sql
SELECT DISTINCT issue_name
FROM public.campaign_top_links
WHERE url ILIKE ANY(:PATS)
ORDER BY issue_name;
```

**Used for:** Filtering downstream queries to game-linked campaigns only.

---

### 2. Per-campaign game clicks

Deduplicates `(issue_name, issue_date)` pairs before summing to avoid double-counting the same link on the same date.

```sql
SELECT issue_name,
       SUM(clicks)     AS clicks,
       MIN(issue_date) AS first_date,
       MAX(issue_date) AS last_date
FROM (
    SELECT issue_name, issue_date, clicks,
           ROW_NUMBER() OVER (
               PARTITION BY issue_name, issue_date
               ORDER BY clicks DESC
           ) AS rn
    FROM public.campaign_top_links
    WHERE url ILIKE ANY(:PATS)
) t
WHERE rn = 1
GROUP BY issue_name;
```

**Output:** `clicks` = total game URL clicks per campaign.  
**Used for:** `game_clicks` per campaign row.

---

### 3. Campaign metadata

```sql
SELECT title,
       sent_at,
       targeted,
       delivered,
       opens,
       unique_opens,
       unique_clicks,
       unsubscribes,
       open_rate_pct,
       ctr_pct
FROM public.newsletter_campaigns
WHERE title = ANY(:game_issues)
ORDER BY sent_at DESC NULLS LAST;
```

**Column usage:**

| Column | Used for |
|---|---|
| `opens` | "Total Opens" KPI card, campaign table, campaign detail, funnel step (non-unique total) |
| `unique_opens` | Rate calculations only (`avg_open_rate`, `game_ctr`) — **never displayed directly** |
| `targeted` | Recipients |
| `open_rate_pct` | Displayed open rate per campaign |
| `ctr_pct` | Displayed click rate per campaign |

---

### 4. Funnel totals

```sql
SELECT COALESCE(SUM(targeted),      0) AS recipients,
       COALESCE(SUM(opens),         0) AS total_opens,
       COALESCE(SUM(unique_opens),  0) AS unique_opens,
       COALESCE(SUM(unique_clicks), 0) AS total_clicks,
       COALESCE(SUM(unsubscribes),  0) AS unsubs
FROM public.newsletter_campaigns
WHERE title = ANY(:game_issues);
```

**Derived metrics:**
- `total_opens` → "Total Opens" KPI card, campaign table, campaign detail, and AllHealthy funnel step
- `avg_open_rate = unique_opens / recipients` (unique_opens used for rates only — never displayed)
- `game_ctr = total_unique_game_clickers / unique_opens` (per campaign: campaign_unique_clickers / campaign_unique_opens)
- All three brands use their non-unique total opens column for the funnel step: SA → `TotalOpened`, AH → `opens`, Ageist → `total_opens`

---

### 5. Total game clicks

```sql
SELECT SUM(clicks) AS total_clicks
FROM (
    SELECT clicks,
           ROW_NUMBER() OVER (
               PARTITION BY issue_name, issue_date
               ORDER BY clicks DESC
           ) AS rn
    FROM public.campaign_top_links
    WHERE url ILIKE ANY(:PATS)
) t
WHERE rn = 1;
```

**Used for:** `total_game_clicks` KPI card.

---

### 6. Per-campaign unique game clickers — PRIMARY ✦

> Used when `allhealthy_contact_clicks` exists.  
> `mailing_name` in `allhealthy_contact_clicks` = `title` in `newsletter_campaigns`.  
> `data::text` cast matches URL patterns against the full JSON text representation.

```sql
SELECT mailing_name,
       COUNT(DISTINCT email) AS game_unique_dedup
FROM public.allhealthy_contact_clicks
WHERE data::text ILIKE ANY(:PATS)
  AND mailing_name IS NOT NULL
GROUP BY mailing_name;
```

**Output:** Distinct email addresses per campaign that clicked a games URL.  
**Used for:** `game_unique` per campaign row.

---

### 7. Total unique game clickers across all AH games campaigns — PRIMARY ✦

> One email that clicked games links in two different campaigns is counted **once** here.

```sql
SELECT COUNT(DISTINCT email) AS total_game_unique_dedup
FROM public.allhealthy_contact_clicks
WHERE data::text ILIKE ANY(:PATS);
```

**Used for:** `total_game_unique` KPI card.  
**Derived:** `game_ctr = total_game_unique_dedup / SUM(unique_opens)` (from query 4).

---

### 8. Monthly unique game clickers — PRIMARY ✦

> Deduped within each calendar month.  
> Joins to `newsletter_campaigns` to get `sent_at` for the month bucket.

```sql
SELECT DATE_TRUNC('month', nc.sent_at)::date AS month,
       COUNT(DISTINCT cc.email)               AS unique_clicks
FROM public.allhealthy_contact_clicks cc
JOIN public.newsletter_campaigns nc
  ON nc.title = cc.mailing_name
WHERE cc.data::text ILIKE ANY(:PATS)
  AND nc.sent_at IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

**Used for:** `monthly_trend.unique_clicks`.

---

### 9. Per-campaign unique game clickers — FALLBACK

> Used only when `allhealthy_contact_clicks` does not exist.  
> These are not deduplicated across campaigns (a subscriber clicking two campaigns = counted twice in the KPI total).

```sql
SELECT issue_name,
       SUM(unique_clicks) AS unique_clicks
FROM (
    SELECT issue_name, issue_date, unique_clicks,
           ROW_NUMBER() OVER (
               PARTITION BY issue_name, issue_date
               ORDER BY clicks DESC
           ) AS rn
    FROM public.campaign_top_links
    WHERE url ILIKE ANY(:PATS)
) t
WHERE rn = 1
GROUP BY issue_name;
```

---

### 10. Monthly clicks trend

```sql
SELECT DATE_TRUNC('month', issue_date)::date AS month,
       SUM(clicks) AS clicks
FROM (
    SELECT issue_date, clicks,
           ROW_NUMBER() OVER (
               PARTITION BY issue_name, issue_date
               ORDER BY clicks DESC
           ) AS rn
    FROM public.campaign_top_links
    WHERE url ILIKE ANY(:PATS)
) t
WHERE rn = 1
GROUP BY 1
ORDER BY 1;
```

**Used for:** `monthly_trend.clicks`.

---

---

## Ageist

Schema: `ageist` (configurable via `AGEIST_SCHEMA`).  
Tables: `ageist_campaigns`, `ageist_campaign_articles`, `ageist_clicks` (optional).  
Patterns matched on `final_url` only for both article and click rows.  
Unique clickers use `email_address` only — no `subscriber_hash` fallback.

---

### 1. Campaign + game article summary

Identifies campaigns with games links via article rows and aggregates click totals.

```sql
WITH game_articles AS (
    SELECT a.campaign_id,
           SUM(COALESCE(a.total_clicks,  0)) AS game_clicks,
           SUM(COALESCE(a.unique_clicks, 0)) AS game_unique_summary,
           MAX(a.last_click)                  AS last_game_click
    FROM ageist.ageist_campaign_articles a
    WHERE COALESCE(a.final_url, '') ILIKE ANY(:PATS)
    GROUP BY a.campaign_id
)
SELECT c.campaign_id,
       c.campaign_title,
       c.subject_line,
       c.send_time,
       c.archive_url,
       c.long_archive_url,
       c.emails_sent,
       c.total_opens,
       c.unique_opens,
       c.open_rate,
       c.total_clicks,
       c.click_rate,
       c.unsubscribed,
       ga.game_clicks,
       ga.game_unique_summary,
       ga.last_game_click
FROM ageist.ageist_campaigns c
JOIN game_articles ga ON ga.campaign_id = c.campaign_id
ORDER BY c.send_time DESC NULLS LAST;
```

**`game_unique_summary`** is the fallback unique value when `ageist_clicks` is unavailable.

---

### 2. Monthly game clicks (article summary)

```sql
SELECT DATE_TRUNC('month',
           COALESCE(a.campaign_send_time, c.send_time))::date AS month,
       SUM(COALESCE(a.total_clicks,  0)) AS clicks,
       SUM(COALESCE(a.unique_clicks, 0)) AS unique_clicks_summary
FROM ageist.ageist_campaign_articles a
JOIN ageist.ageist_campaigns c ON c.campaign_id = a.campaign_id
WHERE COALESCE(a.final_url, '') ILIKE ANY(:PATS)
  AND COALESCE(a.campaign_send_time, c.send_time) IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

---

### 3. Per-campaign unique game clickers — PRIMARY ✦

> Deduplicates on `email_address` only within each campaign.

```sql
SELECT ck.campaign_id,
       COUNT(DISTINCT NULLIF(LOWER(TRIM(ck.email_address)), '')) AS game_unique_dedup
FROM ageist.ageist_clicks ck
WHERE COALESCE(ck.final_url, '') ILIKE ANY(:PATS)
  AND NULLIF(LOWER(TRIM(ck.email_address)), '') IS NOT NULL
GROUP BY ck.campaign_id;
```

**Used for:** `game_unique` per campaign row.

---

### 4. Total unique game clickers across all Ageist campaigns — PRIMARY ✦

> One subscriber clicking two Ageist games campaigns counts once here.

```sql
SELECT COUNT(DISTINCT NULLIF(LOWER(TRIM(ck.email_address)), '')) AS total_game_unique_dedup
FROM ageist.ageist_clicks ck
WHERE COALESCE(ck.final_url, '') ILIKE ANY(:PATS)
  AND NULLIF(LOWER(TRIM(ck.email_address)), '') IS NOT NULL;
```

**Used for:** `total_game_unique` KPI card.

---

### 5. Monthly unique game clickers — PRIMARY ✦

```sql
SELECT DATE_TRUNC('month', ck.campaign_send_time)::date AS month,
       COUNT(DISTINCT NULLIF(LOWER(TRIM(ck.email_address)), '')) AS unique_clicks
FROM ageist.ageist_clicks ck
WHERE COALESCE(ck.final_url, '') ILIKE ANY(:PATS)
  AND ck.campaign_send_time IS NOT NULL
  AND NULLIF(LOWER(TRIM(ck.email_address)), '') IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

**Used for:** `monthly_trend.unique_clicks`.

---

### 6. Fallback — unique clickers from article summary

> Used only when `ageist_clicks` table does not exist.  
> `game_unique_summary` from query 1 is used directly for per-campaign values and summed for the KPI total.  
> **Not cross-campaign deduped** — a subscriber in two campaigns is counted twice.

---

## URL Pattern Reference

| Pattern | Matches |
|---|---|
| `%games.superage.com%` | Direct games domain links |
| `%o.superage.com/r?dest=games.superage.com%` | Redirect links via SuperAge tracking domain |

Applied via `ILIKE ANY(ARRAY[...])`. For Ageist, also matched against URL-encoded variants in `raw_url` / `final_url`.

---

## Unique Clicker Source Summary

| Brand | Primary source | Join key | Deduplication |
|---|---|---|---|
| SuperAge | `Campaigns_Clicks."EmailAddress "` | — | Per-campaign `COUNT(DISTINCT email)` |
| AllHealthy | `allhealthy_contact_clicks.email` | `mailing_name = newsletter_campaigns.title` | Cross-campaign `COUNT(DISTINCT email)` |
| Ageist | `ageist_clicks.email_address` | `campaign_id` | Cross-campaign `COUNT(DISTINCT NULLIF(LOWER(TRIM(email_address)), ''))` |

**Fallback for AllHealthy:** `campaign_top_links.unique_clicks` (not cross-campaign deduped).  
**Fallback for Ageist:** `ageist_campaign_articles.unique_clicks` where `final_url ILIKE ANY(:PATS)` (not cross-campaign deduped).
