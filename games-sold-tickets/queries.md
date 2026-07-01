# Games Sold Tickets — All SQL Queries

All queries run against **PostgreSQL (RDS)**. Dynamic column names (e.g. `{t_oid}`, `{t_type}`) are resolved at runtime by the lambda via column discovery. The concrete column names for `public.games_tickets` as of June 2026 are:

| Placeholder | Actual column |
|---|---|
| `{t_oid}` | `oid` |
| `{t_type}` | `ticket_type` |
| `{t_email}` | `email` |
| `{t_dob}` | `date_of_birth` |
| `{t_gender}` | `gender` |
| `{t_city}` | `city` |
| `{t_date}` | `created_at` |

---

## Section 1 — Sales Tickets

### KPI: Total ticket count
```sql
SELECT COUNT(*) AS n
FROM public.games_tickets;
```

### KPI: Total valid waitlist signups
```sql
SELECT COUNT(DISTINCT email) AS n
FROM public.waitlist_emails
WHERE email_oversight_result NOT IN ('Bot','Undeliverable','Malformed','SpamTrap')
  AND is_suppressed = false;
```

### KPI: Waitlist buyers (ticket buyers who were on the waitlist)
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

### Chart: Ticket type breakdown (with waitlist overlap)
```sql
SELECT
    COALESCE(ticket_type, 'Unknown') AS type,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE email IN (
        SELECT DISTINCT ON (email) email
        FROM public.waitlist_emails
        WHERE email_oversight_result NOT IN ('Bot','Undeliverable','Malformed','SpamTrap')
          AND is_suppressed = false
        ORDER BY email, created_at ASC
    )) AS on_waitlist
FROM public.games_tickets
GROUP BY 1
ORDER BY 2 DESC;
```

### Chart: Age distribution of ticket buyers
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

### Chart: Gender distribution
```sql
SELECT
    COALESCE(INITCAP(gender::text), 'Unknown') AS gender,
    COUNT(*) AS count
FROM public.games_tickets
GROUP BY 1
ORDER BY 2 DESC;
```

### Chart: Top cities
```sql
SELECT
    COALESCE(city, 'Unknown') AS city,
    COUNT(*) AS count
FROM public.games_tickets
WHERE city IS NOT NULL AND TRIM(city) != ''
GROUP BY 1
ORDER BY 2 DESC
LIMIT 10;
```

### Table: Most recent purchases
```sql
SELECT *
FROM public.games_tickets
ORDER BY created_at DESC
LIMIT 20;
```

### KPI: Estimated revenue (ticket prices hardcoded: Athlete Pass $497, Champion Pass $2997, Spectator Pass $97)
```sql
SELECT
    LOWER(COALESCE(ticket_type, '')) AS type,
    COUNT(*) AS n
FROM public.games_tickets
GROUP BY 1;
```

### Chart: Landing by source — Our Brands
```sql
SELECT utm_source AS source, COUNT(*) AS count
FROM superage.games_landing_events
WHERE utm_source = ANY(ARRAY[
    'superage','ageist','allhealthy','healthbrief','agent',
    'fitnessquiz','optimism_team','david_stewart','optimism'
])
  AND NOT (utm_source = ANY(ARRAY['superage','ageist','allhealthy','healthbrief'])
           AND utm_medium = 'email')
GROUP BY utm_source
ORDER BY count DESC;
```

### Chart: Landing by source — Sponsors
```sql
SELECT utm_source AS source, COUNT(*) AS count
FROM superage.games_landing_events
WHERE utm_source = ANY(ARRAY[
    'buck_institute','pvolve','pur','altra','whocp','braun','junco'
])
GROUP BY utm_source
ORDER BY count DESC;
```

### Chart: Landing by source — Event Partners
```sql
SELECT LOWER(TRIM(utm_source)) AS source, COUNT(*) AS count
FROM superage.games_landing_events
WHERE LOWER(TRIM(utm_source)) = ANY(ARRAY[
    'cat_5','adventure_women','global_wellness','the_pump','lifetime'
])
GROUP BY 1
ORDER BY count DESC;
```

### Chart: Landing events by campaign
```sql
SELECT utm_campaign AS campaign, COUNT(*) AS count
FROM superage.games_landing_events
WHERE utm_source = ANY(ARRAY[
    'superage','ageist','allhealthy','healthbrief','agent',
    'fitnessquiz','optimism_team','david_stewart','optimism'
])
  AND NOT (utm_source = ANY(ARRAY['superage','ageist','allhealthy','healthbrief'])
           AND utm_medium = 'email')
  AND utm_campaign IS NOT NULL AND TRIM(utm_campaign) != ''
GROUP BY utm_campaign
ORDER BY count DESC;
```

### Chart: Landing events by medium
```sql
SELECT utm_medium AS medium, COUNT(*) AS count
FROM superage.games_landing_events
WHERE utm_source = ANY(ARRAY[
    'superage','ageist','allhealthy','healthbrief','agent',
    'fitnessquiz','optimism_team','david_stewart','optimism'
])
  AND NOT (utm_source = ANY(ARRAY['superage','ageist','allhealthy','healthbrief'])
           AND utm_medium = 'email')
  AND utm_medium IS NOT NULL AND TRIM(utm_medium) != ''
GROUP BY utm_medium
ORDER BY count DESC;
```

### Chart: Landing events daily trend
```sql
SELECT date::date AS day, COUNT(*) AS count
FROM superage.games_landing_events
WHERE utm_source = ANY(ARRAY[
    'superage','ageist','allhealthy','healthbrief','agent',
    'fitnessquiz','optimism_team','david_stewart','optimism'
])
  AND NOT (utm_source = ANY(ARRAY['superage','ageist','allhealthy','healthbrief'])
           AND utm_medium = 'email')
  AND date IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

### KPI: SuperAge raw email clicks
```sql
SELECT COUNT(*) AS n
FROM superage."Campaigns_Clicks"
WHERE "URL" ILIKE ANY(ARRAY['%superage.com/games%','%superagegames%']);
```

### KPI: Ageist raw email clicks
```sql
SELECT COUNT(*) AS n
FROM ageist.ageist_clicks
WHERE COALESCE(final_url, '') ILIKE ANY(ARRAY['%superage.com/games%','%superagegames%'])
  AND NULLIF(LOWER(TRIM(email_address)), '') IS NOT NULL;
```

### KPI: HealthBrief raw email clicks
```sql
SELECT COUNT(*) AS n
FROM optimism.healthbrief_contact_activity
WHERE type = 'click'
  AND data ILIKE ANY(ARRAY['%superage.com/games%','%superagegames%'])
  AND mailing_name NOT ILIKE '%[TEST]%'
  AND bot = 'No';
```

### KPI: AllHealthy raw email clicks
```sql
SELECT COUNT(*) AS n
FROM public.allhealthy_contact_clicks
WHERE data::text ILIKE ANY(ARRAY['%superage.com/games%','%superagegames%'])
  AND bot = 'No';
```

---

## Section 2 — Buyer Persona

> Joins `public.games_tickets` × `superage.subscriber_quiz` on `oid`.
> `quiz_deduped` keeps only the most recent quiz entry per buyer.

### Reusable CTEs (used in all persona queries below)
```sql
WITH buyer_oids AS (
    SELECT DISTINCT LOWER(TRIM(oid)) AS oid
    FROM public.games_tickets
    WHERE oid IS NOT NULL AND TRIM(oid) != ''
),
quiz_deduped AS (
    SELECT DISTINCT ON (LOWER(TRIM(sq.oid)))
        LOWER(TRIM(sq.oid))     AS oid,
        sq.longevity_score,
        sq.age,
        sq.gender,
        sq.financial_situation,
        sq.education_level,
        sq.marital_status,
        sq.sleep_hours,
        sq.smoking_status,
        sq.is_obese,
        sq.alcohol_freq,
        sq.stress_impact,
        COALESCE(sq.exercise_freq, sq.exercise_freq_male,
                 sq.exercise_freq_female, sq.exercise_freq_other) AS exercise_freq
    FROM superage.subscriber_quiz sq
    WHERE sq.oid IS NOT NULL AND TRIM(sq.oid) != ''
    ORDER BY LOWER(TRIM(sq.oid)), sq.created_at DESC
)
```

### KPI: Unique buyers (distinct oids in games_tickets)
```sql
SELECT COUNT(DISTINCT LOWER(TRIM(oid))) AS n
FROM public.games_tickets
WHERE oid IS NOT NULL AND TRIM(oid) != '';
```

### KPI: Matched buyers + avg longevity + avg age
```sql
-- Prepend the reusable CTEs above
SELECT
    COUNT(*) AS matched,
    ROUND(AVG(qd.longevity_score)::numeric, 2) AS avg_longevity,
    ROUND(AVG(qd.age)::numeric, 1)             AS avg_age
FROM buyer_oids be
INNER JOIN quiz_deduped qd ON be.oid = qd.oid;
```

### Chart: Longevity score buckets
```sql
-- Prepend the reusable CTEs above
SELECT
    CASE
        WHEN qd.longevity_score < 70 THEN '<70'
        WHEN qd.longevity_score < 80 THEN '70-79'
        WHEN qd.longevity_score < 90 THEN '80-89'
        ELSE '90+'
    END AS bucket,
    COUNT(*) AS count
FROM buyer_oids be
INNER JOIN quiz_deduped qd ON be.oid = qd.oid
WHERE qd.longevity_score IS NOT NULL
GROUP BY 1
ORDER BY MIN(qd.longevity_score);
```

### Charts: Persona attribute distributions
> Replace `{col}` with each attribute. `Not specified` is shown instead of filtering nulls.

**Attributes:** `gender`, `financial_situation`, `education_level`, `marital_status`,
`sleep_hours`, `exercise_freq`, `smoking_status`, `is_obese`, `alcohol_freq`, `stress_impact`

```sql
-- Prepend the reusable CTEs above
SELECT
    CASE
        WHEN qd.{col} IS NULL OR TRIM(qd.{col}::text) = '' THEN 'Not specified'
        ELSE qd.{col}::text
    END AS val,
    COUNT(*) AS count
FROM buyer_oids be
INNER JOIN quiz_deduped qd ON be.oid = qd.oid
GROUP BY 1
ORDER BY 2 DESC;
```

**Concrete example — Gender:**
```sql
WITH buyer_oids AS (
    SELECT DISTINCT LOWER(TRIM(oid)) AS oid
    FROM public.games_tickets
    WHERE oid IS NOT NULL AND TRIM(oid) != ''
),
quiz_deduped AS (
    SELECT DISTINCT ON (LOWER(TRIM(sq.oid)))
        LOWER(TRIM(sq.oid)) AS oid,
        sq.gender
    FROM superage.subscriber_quiz sq
    WHERE sq.oid IS NOT NULL AND TRIM(sq.oid) != ''
    ORDER BY LOWER(TRIM(sq.oid)), sq.created_at DESC
)
SELECT
    CASE WHEN qd.gender IS NULL OR TRIM(qd.gender) = '' THEN 'Not specified'
         ELSE qd.gender END AS val,
    COUNT(*) AS count
FROM buyer_oids be
INNER JOIN quiz_deduped qd ON be.oid = qd.oid
GROUP BY 1 ORDER BY 2 DESC;
```

### KPI: Average longevity score — all quiz takers (benchmark)
```sql
SELECT ROUND(AVG(longevity_score)::numeric, 1) AS avg_ls
FROM superage.subscriber_quiz
WHERE longevity_score IS NOT NULL;
```

### Chart: By ticket type — matched buyers breakdown
> Shows count of matched buyers, avg longevity score, and avg age per ticket type.

```sql
-- Prepend the reusable CTEs above
SELECT
    COALESCE(gt.ticket_type, 'Unknown') AS ticket_type,
    COUNT(*) AS count,
    ROUND(AVG(qd.longevity_score)::numeric, 1) AS avg_longevity,
    ROUND(AVG(qd.age)::numeric, 1) AS avg_age
FROM public.games_tickets gt
INNER JOIN buyer_oids be ON LOWER(TRIM(gt.oid)) = be.oid
INNER JOIN quiz_deduped qd ON be.oid = qd.oid
GROUP BY 1 ORDER BY 2 DESC;
```

### Chart: Unmatched buyers — gender breakdown
> Buyers whose oid does not appear in `superage.subscriber_quiz`. Counts distinct buyers (not tickets).

```sql
SELECT
    COALESCE(INITCAP(gender::text), 'Unknown') AS gender,
    COUNT(DISTINCT LOWER(TRIM(oid))) AS count
FROM public.games_tickets
WHERE LOWER(TRIM(oid)) NOT IN (
    SELECT DISTINCT LOWER(TRIM(oid)) FROM superage.subscriber_quiz
    WHERE oid IS NOT NULL AND TRIM(oid) != ''
)
GROUP BY 1 ORDER BY 2 DESC;
```

### Chart: Unmatched buyers — age breakdown
> Same unmatched population, broken into age buckets from `date_of_birth`. Counts distinct buyers.

```sql
SELECT
    CASE
        WHEN DATE_PART('year', AGE(date_of_birth)) < 45 THEN 'Under 45'
        WHEN DATE_PART('year', AGE(date_of_birth)) < 55 THEN '45-54'
        WHEN DATE_PART('year', AGE(date_of_birth)) < 65 THEN '55-64'
        WHEN DATE_PART('year', AGE(date_of_birth)) < 75 THEN '65-74'
        ELSE '75+'
    END AS range,
    COUNT(DISTINCT LOWER(TRIM(oid))) AS count
FROM public.games_tickets
WHERE date_of_birth IS NOT NULL
  AND LOWER(TRIM(oid)) NOT IN (
    SELECT DISTINCT LOWER(TRIM(oid)) FROM superage.subscriber_quiz
    WHERE oid IS NOT NULL AND TRIM(oid) != ''
  )
GROUP BY 1 ORDER BY MIN(DATE_PART('year', AGE(date_of_birth)));
```

---

## Section 3 — Funnel Analysis

> Joins `public.games_tickets` × `superage.ticket_transactions` on `transaction_id`.
> Champion Pass has no `transaction_id` and does not appear in matched data.
> UTM columns from `games_tickets` are **excluded** (corrupted); session data comes from `ticket_transactions` only.

### KPI: Matched tickets (tickets with a transaction record)
```sql
SELECT COUNT(*) AS matched
FROM public.games_tickets gt
INNER JOIN superage.ticket_transactions tt
    ON gt.transaction_id = tt.transaction_id
WHERE gt.transaction_id IS NOT NULL
  AND TRIM(gt.transaction_id) != '';
```

### Chart: By session medium per ticket type
```sql
SELECT
    COALESCE(gt.ticket_type, 'Unknown') AS ticket_type,
    COALESCE(NULLIF(TRIM(tt.session_medium), ''), '(none)') AS session_medium,
    COUNT(*) AS count
FROM public.games_tickets gt
INNER JOIN superage.ticket_transactions tt
    ON gt.transaction_id = tt.transaction_id
WHERE gt.transaction_id IS NOT NULL AND TRIM(gt.transaction_id) != ''
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

### Chart: By session source per ticket type
> `ig` and `l.instagram.com` are normalized to `Instagram`.

```sql
SELECT
    COALESCE(gt.ticket_type, 'Unknown') AS ticket_type,
    CASE
        WHEN LOWER(TRIM(tt.session_source)) IN ('ig', 'l.instagram.com', 'instagram') THEN 'Instagram'
        ELSE COALESCE(NULLIF(TRIM(tt.session_source), ''), '(direct)')
    END AS session_source,
    COUNT(*) AS count
FROM public.games_tickets gt
INNER JOIN superage.ticket_transactions tt
    ON gt.transaction_id = tt.transaction_id
WHERE gt.transaction_id IS NOT NULL AND TRIM(gt.transaction_id) != ''
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

### Chart: By campaign per ticket type (with source prefix)
> Instagram variants merged. Parenthesized campaign names normalized (e.g. `(referral)` → `Referral`).

```sql
SELECT
    COALESCE(gt.ticket_type, 'Unknown') AS ticket_type,
    CASE
        WHEN LOWER(TRIM(tt.session_source)) IN ('ig', 'l.instagram.com', 'instagram') THEN 'Instagram'
        ELSE COALESCE(NULLIF(TRIM(tt.session_source), ''), '(direct)')
    END AS session_source,
    CASE
        WHEN LOWER(TRIM(tt.session_campaign)) IN ('referral', '(referral)') THEN 'Referral'
        WHEN LOWER(TRIM(tt.session_campaign)) IN ('organic',  '(organic)')  THEN 'Organic'
        WHEN LOWER(TRIM(tt.session_campaign)) IN ('direct',   '(direct)')   THEN 'Direct'
        ELSE COALESCE(NULLIF(TRIM(tt.session_campaign), ''), '(not set)')
    END AS session_campaign,
    COUNT(*) AS count
FROM public.games_tickets gt
INNER JOIN superage.ticket_transactions tt
    ON gt.transaction_id = tt.transaction_id
WHERE gt.transaction_id IS NOT NULL AND TRIM(gt.transaction_id) != ''
GROUP BY 1, 2, 3
ORDER BY 1, 4 DESC;
```

---

## Label Mapping Reference

The following source/medium values are remapped in the dashboard UI for executive presentation:

| Raw value | Display label |
|---|---|
| `ig` | Instagram |
| `l.instagram.com` | Instagram |
| `superage` | SuperAge |
| `ageist` | Ageist |
| `allhealthy` | AllHealthy |
| `healthbrief` | HealthBrief |
| `adventure_women` | Adventure Women |
| `global_wellness` | Global Wellness |
| `the_pump` | The Pump |
| `fitnessquiz` | Fitness Quiz |
| `optimism_team` | Optimism Team |
| `david_stewart` | David Stewart |
| `(none)` | None |
| `(not set)` | Not Set |
| `(direct)` | Direct |
| `(organic)` | Organic |
| `(referral)` | Referral |
| `pop-up` | Pop-Up |
