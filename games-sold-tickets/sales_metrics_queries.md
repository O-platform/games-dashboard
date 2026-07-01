# sales_metrics.json — Query Reference

All queries executed by the Sales Metrics Lambda that produces `sales_metrics.json`.

---

## Data Source Architecture

Landing-page traffic is sourced from two layers:

| Layer | Source | Brands covered | Why |
|---|---|---|---|
| Raw email clicks | `superage."Campaigns_Clicks"`, `allhealthy_contact_clicks`, `ageist.ageist_clicks` | superage, allhealthy, ageist | Actual email click volume; more accurate than landing-page visit syncs |
| Filtered landing events | `superage.games_landing_events` (excluding SA/AH/AG email rows) | healthbrief, optimism, sponsors, website, referral | No dedicated raw click tables for these sources |

**Exclusion rule applied to `games_landing_events` throughout all landing queries:**

```sql
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email')
```

---

## URL Pattern Reference

Applied via `ILIKE ANY(ARRAY[...])` across all raw click tables.

| Pattern | Matches |
|---|---|
| `%games.superage.com%` | Direct games domain links |
| `%o.superage.com/r?dest=games.superage.com%` | Redirect links via SuperAge tracking domain |

---

## Section 1 — Ticket and Waitlist Queries

DB: Main (`DB_SECRET_ARN`), `public` schema.

---

### 1. Total ticket count

```sql
SELECT COUNT(*) AS n
FROM public.games_tickets;
```

Used for: `total_tickets` KPI.

---

### 2. Total valid waitlist emails

```sql
SELECT COUNT(DISTINCT email) AS n
FROM public.waitlist_emails
WHERE email_oversight_result NOT IN ('Bot','Undeliverable','Malformed','SpamTrap')
  AND is_suppressed = false;
```

Used for: `total_waitlist` KPI.

---

### 3. Waitlist buyers (crossover)

```sql
SELECT COUNT(*) AS n
FROM public.games_tickets t
WHERE email IN (
    SELECT DISTINCT ON (email) email
    FROM public.waitlist_emails
    WHERE email_oversight_result NOT IN ('Bot','Undeliverable','Malformed','SpamTrap')
      AND is_suppressed = false
    ORDER BY email, created_at ASC
);
```

Used for: `waitlist_buyers`. `conversion_rate = waitlist_buyers / total_waitlist`.

---

### 4. Ticket types with waitlist overlap

```sql
SELECT COALESCE(ticket_type, 'Unknown') AS type,
       COUNT(*) AS total,
       COUNT(*) FILTER (WHERE email IN (
           SELECT DISTINCT ON (email) email
           FROM public.waitlist_emails
           WHERE email_oversight_result NOT IN ('Bot','Undeliverable','Malformed','SpamTrap')
             AND is_suppressed = false
           ORDER BY email, created_at ASC
       )) AS on_waitlist
FROM public.games_tickets
GROUP BY 1 ORDER BY 2 DESC;
```

Used for: `ticket_types` — each row has `type`, `total`, `on_waitlist`, `direct = total - on_waitlist`.

---

### 5. Age distribution

```sql
SELECT
  CASE
    WHEN DATE_PART('year', AGE(date_of_birth)) < 35 THEN 'Under 35'
    WHEN DATE_PART('year', AGE(date_of_birth)) < 45 THEN '35-44'
    WHEN DATE_PART('year', AGE(date_of_birth)) < 55 THEN '45-54'
    WHEN DATE_PART('year', AGE(date_of_birth)) < 65 THEN '55-64'
    WHEN DATE_PART('year', AGE(date_of_birth)) < 75 THEN '65-74'
    ELSE '75+'
  END AS range,
  COUNT(*) AS count
FROM public.games_tickets
WHERE date_of_birth IS NOT NULL
GROUP BY 1
ORDER BY MIN(DATE_PART('year', AGE(date_of_birth)));
```

Used for: `age_distribution`.

---

### 6. Gender distribution

```sql
SELECT COALESCE(INITCAP(gender::text), 'Unknown') AS gender,
       COUNT(*) AS count
FROM public.games_tickets
GROUP BY 1 ORDER BY 2 DESC;
```

Used for: `gender_distribution`.

---

### 7. City distribution (top 10)

```sql
SELECT COALESCE(city, 'Unknown') AS city, COUNT(*) AS count
FROM public.games_tickets
WHERE city IS NOT NULL AND TRIM(city) != ''
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

Used for: `city_distribution`.

---

### 8. Estimated revenue

```sql
SELECT LOWER(COALESCE(ticket_type, '')) AS type, COUNT(*) AS n
FROM public.games_tickets
GROUP BY 1;
```

Multiplied in Python by: `champion pass = $1,300`, `athlete pass = $400`.
Used for: `estimated_revenue`.

---

### 9. Recent tickets

```sql
SELECT *
FROM public.games_tickets
ORDER BY created_at DESC
LIMIT 20;
```

Used for: `recent_tickets.rows`.

---

## Section 2 — Filtered Landing Events

From `superage.games_landing_events`, excluding SA/AH/AG email rows.

---

### 10. Total filtered landing events

```sql
SELECT COUNT(*) AS n
FROM superage.games_landing_events
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email');
```

Used for: component of `total_landing` (added to SA + AH + AG raw totals).

---

### 11. Remaining brand sources (healthbrief, optimism)

```sql
SELECT utm_source AS source, COUNT(*) AS count
FROM superage.games_landing_events
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email')
  AND utm_source IN ('healthbrief', 'optimism')
  AND utm_source IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;
```

Used for: healthbrief and optimism entries in `landing_by_source_brands`.

---

### 12. Sponsor count

```sql
SELECT COUNT(*) AS n
FROM superage.games_landing_events
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email')
  AND utm_source <> ALL(ARRAY['superage','allhealthy','healthbrief','ageist','optimism'])
  AND utm_source IS NOT NULL AND TRIM(utm_source) != '';
```

Used for: `landing_sponsors` KPI.

---

### 13. Sponsor by source — top 5

```sql
SELECT utm_source AS source, COUNT(*) AS count
FROM superage.games_landing_events
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email')
  AND utm_source <> ALL(ARRAY['superage','allhealthy','healthbrief','ageist','optimism'])
  AND utm_source IS NOT NULL AND TRIM(utm_source) != ''
GROUP BY 1 ORDER BY 2 DESC LIMIT 5;
```

Used for: `landing_by_source_sponsors`.

---

### 14. Filtered by campaign

```sql
SELECT utm_campaign AS campaign, COUNT(*) AS count
FROM superage.games_landing_events
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email')
  AND utm_campaign IS NOT NULL AND TRIM(utm_campaign) != ''
GROUP BY 1 ORDER BY 2 DESC;
```

Used for: component of `landing_by_campaign` merge (covers healthbrief, optimism, sponsors, website).

---

### 15. Filtered by day

```sql
SELECT date::date AS day, COUNT(*) AS count
FROM superage.games_landing_events
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email')
  AND date IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

Used for: component of `landing_by_day` merge.

---

### 16. Filtered by medium

```sql
SELECT utm_medium AS medium, COUNT(*) AS count
FROM superage.games_landing_events
WHERE NOT (utm_source = ANY(ARRAY['superage','allhealthy','ageist'])
           AND utm_medium = 'email')
  AND utm_medium IS NOT NULL AND TRIM(utm_medium) != ''
GROUP BY 1 ORDER BY 2 DESC;
```

Used for: base of `landing_by_medium`. SA + AH + AG raw totals are added to the `email` bucket in Python after this query.

---

## Section 3 — SuperAge Raw Clicks

DB: Main (`DB_SECRET_ARN`), schema `superage`.
Table: `superage."Campaigns_Clicks"`
Filter: `"URL" ILIKE ANY(:PATS)`
Campaign column: `issue_name`
Date column: `"Date"` (timestamp)

---

### 17. SA total game clicks

```sql
SELECT COUNT(*) AS n
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(:PATS);
```

Used for: `sa_total` → `landing_by_source_brands["superage"]`, component of `total_landing`.

---

### 18. SA by campaign

```sql
SELECT issue_name AS campaign, COUNT(*) AS count
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(:PATS)
  AND issue_name IS NOT NULL
GROUP BY issue_name
ORDER BY count DESC;
```

Used for: component of `landing_by_campaign` merge.

---

### 19. SA by day

```sql
SELECT "Date"::date AS day, COUNT(*) AS count
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(:PATS)
  AND "Date" IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

Used for: component of `landing_by_day` merge.

---

## Section 4 — AllHealthy Raw Clicks

DB: AllHealthy (`AH_DB_*` env vars).
Table: `public.allhealthy_contact_clicks`
Filter: `data::text ILIKE ANY(:PATS)`
Campaign column: `mailing_name`
Date column: `event_timestamp` (timestamp)
Falls back to zeros if AH DB is unreachable — lambda continues with a warning log.

---

### 20. AH total game clicks

```sql
SELECT COUNT(*) AS n
FROM public.allhealthy_contact_clicks
WHERE data::text ILIKE ANY(:PATS);
```

Used for: `ah_total` → `landing_by_source_brands["allhealthy"]`, component of `total_landing`.

---

### 21. AH by campaign

```sql
SELECT mailing_name AS campaign, COUNT(*) AS count
FROM public.allhealthy_contact_clicks
WHERE data::text ILIKE ANY(:PATS)
  AND mailing_name IS NOT NULL
GROUP BY mailing_name ORDER BY count DESC;
```

Used for: component of `landing_by_campaign` merge.

---

### 22. AH by day

```sql
SELECT event_timestamp::date AS day, COUNT(*) AS count
FROM public.allhealthy_contact_clicks
WHERE data::text ILIKE ANY(:PATS)
  AND event_timestamp IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

Used for: component of `landing_by_day` merge.

---

## Section 5 — Ageist Raw Clicks

DB: Main (`DB_SECRET_ARN`), schema `ageist`.
Tables: `ageist.ageist_clicks` joined to `ageist.ageist_campaigns` for campaign title.
Filter: `COALESCE(final_url,'') ILIKE ANY(:PATS)`
Email dedup: `NULLIF(LOWER(TRIM(email_address)),'') IS NOT NULL`
Campaign column: `ageist_campaigns.campaign_title` (via `campaign_id` join)
Date column: `campaign_send_time` — no per-click timestamp available; all clicks within a campaign share the campaign send date.

---

### 23. AG total game clicks

```sql
SELECT COUNT(*) AS n
FROM ageist.ageist_clicks
WHERE COALESCE(final_url, '') ILIKE ANY(:PATS)
  AND NULLIF(LOWER(TRIM(email_address)), '') IS NOT NULL;
```

Used for: `ag_total` → `landing_by_source_brands["ageist"]`, component of `total_landing`.

---

### 24. AG by campaign

```sql
SELECT c.campaign_title AS campaign, COUNT(*) AS count
FROM ageist.ageist_clicks ck
JOIN ageist.ageist_campaigns c ON c.campaign_id = ck.campaign_id
WHERE COALESCE(ck.final_url, '') ILIKE ANY(:PATS)
  AND NULLIF(LOWER(TRIM(ck.email_address)), '') IS NOT NULL
  AND c.campaign_title IS NOT NULL
GROUP BY c.campaign_title
ORDER BY count DESC;
```

Used for: component of `landing_by_campaign` merge.

---

### 25. AG by day

```sql
SELECT ck.campaign_send_time::date AS day, COUNT(*) AS count
FROM ageist.ageist_clicks ck
WHERE COALESCE(ck.final_url, '') ILIKE ANY(:PATS)
  AND ck.campaign_send_time IS NOT NULL
  AND NULLIF(LOWER(TRIM(ck.email_address)), '') IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

Used for: component of `landing_by_day` merge.
Note: No per-click timestamp exists in Ageist. All clicks for a campaign are bucketed on the campaign send date.

---

## Section 6 — Python Merge Logic

After all DB queries complete, the following combines results using `defaultdict(int)`.

---

### `total_landing`

```python
total_landing = sa_total + ah_total + ag_total + filtered_landing_total
```

---

### `landing_our_brands`

```python
landing_our_brands = (
    sa_total + ah_total + ag_total
    + sum(remaining_brand_rows.values())  # healthbrief + optimism counts
)
```

---

### `landing_by_source_brands`

```python
brand_source_dict = {
    "superage":   sa_total,     # from raw
    "allhealthy": ah_total,     # from raw
    "ageist":     ag_total,     # from raw
    # healthbrief, optimism added from filtered landing
}
# sorted descending by count, zeros excluded
```

---

### `landing_by_campaign` (top 10)

All four sources merged into one dict, sorted descending, top 10 kept.

```python
# SA:              issue_name    -> count
# AH:              mailing_name  -> count
# AG:              campaign_title -> count
# Filtered landing: utm_campaign  -> count
```

---

### `landing_by_day`

All four sources merged by ISO date string, sorted ascending.

```python
# SA:              "Date"::date            -> count
# AH:              event_timestamp::date   -> count
# AG:              campaign_send_time::date -> count (one bucket per campaign)
# Filtered landing: date::date             -> count
```

---

### `landing_by_medium`

```python
# Start with filtered landing medium counts
# Then add SA + AH + AG total to the 'email' bucket (all raw brand clicks are email medium)
raw_brand_email_total = sa_total + ah_total + ag_total
filtered_medium_dict["email"] += raw_brand_email_total
# sorted descending, top 10
```

---

## Section 7 — Buyer Persona (Subscriber Quiz Join)

DB: Main (`DB_SECRET_ARN`), schemas `public` + `superage`.
Join: `public.games_tickets` email → `superage.subscriber_quiz` email (case-insensitive LOWER/TRIM).
Dedup: `DISTINCT ON (email) ... ORDER BY email, created_at DESC` — latest quiz entry per person.
Unit of analysis: unique buyer emails (not individual ticket rows).

---

### 26. Unique buyer emails + match count + averages

```sql
WITH buyer_emails AS (
  SELECT DISTINCT LOWER(TRIM(email)) AS email
  FROM public.games_tickets
  WHERE email IS NOT NULL AND TRIM(email) != ''
),
quiz_deduped AS (
  SELECT DISTINCT ON (LOWER(TRIM(sq.email)))
    LOWER(TRIM(sq.email)) AS email,
    sq.longevity_score, sq.age, ...
  FROM superage.subscriber_quiz sq
  WHERE sq.email IS NOT NULL AND TRIM(sq.email) != ''
  ORDER BY LOWER(TRIM(sq.email)), sq.created_at DESC
)
SELECT COUNT(*) AS matched,
       ROUND(AVG(qd.longevity_score)::numeric, 2) AS avg_longevity,
       ROUND(AVG(qd.age)::numeric, 1) AS avg_age
FROM buyer_emails be
INNER JOIN quiz_deduped qd ON be.email = qd.email
```

Used for: `persona.matched_buyers`, `persona.avg_longevity_score`, `persona.avg_age`.

---

### 27–36. Per-dimension distribution queries (same CTE pattern)

Columns queried: `gender`, `financial_situation`, `education_level`, `marital_status`,
`sleep_hours`, `exercise_freq`, `smoking_status`, `is_obese`, `alcohol_freq`, `stress_impact`.

Each returns `{<col>: val, count: n}` array sorted by count DESC.

Used for: `persona.<col>` arrays.

---

### 37. Longevity score buckets

```sql
CASE
  WHEN qd.longevity_score < 70 THEN '<70'
  WHEN qd.longevity_score < 80 THEN '70-79'
  WHEN qd.longevity_score < 90 THEN '80-89'
  ELSE '90+'
END AS bucket
```

Used for: `persona.longevity_buckets`.

---

## Section 8 — Ticket Funnel (Transaction Source Analysis)

DB: Main (`DB_SECRET_ARN`), schemas `public` + `superage`.
Join: `public.games_tickets.transaction_id` = `superage.ticket_transactions.transaction_id`.
Note: UTM columns in `games_tickets` are corrupted — only `session_*` columns from
`ticket_transactions` are used for source attribution.

---

### 38. Total matched tickets

```sql
SELECT COUNT(*) AS matched
FROM public.games_tickets gt
INNER JOIN superage.ticket_transactions tt
  ON gt.transaction_id = tt.transaction_id
WHERE gt.transaction_id IS NOT NULL AND TRIM(gt.transaction_id) != ''
```

Used for: `ticket_funnel.matched_tickets`.

---

### 39. By ticket_type × session_medium

```sql
SELECT COALESCE(gt.ticket_type, 'Unknown') AS ticket_type,
       COALESCE(NULLIF(TRIM(tt.session_medium), ''), '(none)') AS session_medium,
       COUNT(*) AS count
FROM public.games_tickets gt
INNER JOIN superage.ticket_transactions tt ON gt.transaction_id = tt.transaction_id
WHERE gt.transaction_id IS NOT NULL AND TRIM(gt.transaction_id) != ''
GROUP BY 1, 2 ORDER BY 1, 3 DESC
```

Used for: `ticket_funnel.by_type[].by_medium`.

---

### 40. By ticket_type × session_source

Same pattern as #39 but `session_source`, defaulting to `'(direct)'`.
Used for: `ticket_funnel.by_type[].by_source`.

---

### 41. By ticket_type × session_campaign

Same pattern as #39 but `session_campaign`, defaulting to `'(not set)'`.
Rows with `'(not set)'` excluded from frontend display.
Used for: `ticket_funnel.by_type[].by_campaign`.

---

## Output Field Reference

| Field | Formula |
|---|---|
| `total_landing` | SA_raw + AH_raw + AG_raw + filtered_landing_total |
| `landing_our_brands` | SA + AH + AG + healthbrief + optimism |
| `landing_sponsors` | filtered_landing WHERE utm_source NOT IN all 5 brands |
| `landing_by_source_brands` | SA/AH/AG from raw tables + HB/Optimism from filtered landing |
| `landing_by_source_sponsors` | filtered_landing non-brand sources, top 5 |
| `landing_by_campaign` | All 4 sources merged, top 10 by total count |
| `landing_by_medium` | filtered_landing mediums + SA+AH+AG total added to email bucket |
| `landing_by_day` | All 4 sources merged by date, sorted ascending |
| `conversion_rate` | waitlist_buyers / total_waitlist x 100 |
| `direct_buyers` | total_tickets - waitlist_buyers |
| `estimated_revenue` | SUM(ticket_count x price) per ticket type |

---

## DB Connection Summary

| Connection | Env var | Schemas accessed | Fallback on failure |
|---|---|---|---|
| Main | `DB_SECRET_ARN` | `public`, `superage`, `ageist` | Hard fail |
| AllHealthy | `AH_DB_HOST/NAME/USER/PASSWORD` | `public` (AH DB) | Zeros for AH metrics, warning logged |
