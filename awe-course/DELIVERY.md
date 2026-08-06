# AWE Course Dashboard — Delivery & Verification Doc

Everything needed to deploy, verify, and hand off the AWE Course dashboard: data
sources, DB objects (tables + matviews) with DDL, the Lambdas, env vars, schedule,
and — most importantly — **the exact SQL behind every KPI so each number can be
checked directly against RDS**.

- **DB engine:** PostgreSQL (AWS RDS), schema `superage`.
- **Constants used throughout:** per-source click floor — SuperAge & **Ageist**
  from `2026-07-01`, AllHealthy/HealthBrief from `2026-07-27`; read floor
  `AWE_SINCE = 2026-07-01`. URL match `%superage.com/awecourse%` (Ageist uses
  `final_url LIKE '%awecourse%'`), price `$99`. (All timestamps UTC.)
- **Four click brands:** SuperAge, AllHealthy, HealthBrief, Ageist. The Overview
  has a page-wide **brand filter** (All / SuperAge / AllHealthy / HealthBrief /
  Ageist); the "All" unique-clicker count is deduped across brands, so per-brand
  sums exceed it (one person clicking in two brands counts in each).
- **Real members only:** `awe_course_members.is_superage = false`. `is_superage =
  true` marks ~7 internal/team accounts and is excluded **everywhere**.
- **Active waitlist only:** `awe_waitlist.state = 'Active'`. Unsubscribed rows were
  email tests and are excluded everywhere.
- **Null-UTM defaults (per context):** checkout landing → `source=superage,
  medium=website`; purchaser matview & waitlist → `source=superage, medium=email`.
  A null/blank acquisition is treated as a SuperAge signup, never `Unknown`.
- **Landing vs checkout is decided by `product_url`** (never null), NOT by `o_event`:
  a **course main-page landing** has `product_url` containing `awecourse`
  (`https://superage.com/awecourse/`); a **checkout event** has `product_url`
  containing `checkout` (`https://super-age.circle.so/checkout/...`). The metrics use
  `product_url`; the ingest still matches on `o_event`/`utm_campaign` (both always set).
- **Google Ads (SuperAge):** course main-page landings (`product_url` ~ `awecourse`)
  → the **"Landed via Google Ads"** top-of-funnel metric, counted regardless of
  `utm_campaign` (may be null in future). Checkout events with `utm_campaign =
  google_ads_awe` → the **"SuperAge Google Ads"** checkout bar. No `oid` on these rows
  → **non-unique event counts only**. Never folded into "SuperAge Website / Organic".
  Lambda env: `AWE_GOOGLE_ADS_CAMPAIGN=google_ads_awe`,
  `AWE_LANDING_URL_MATCH=awecourse`, `AWE_CHECKOUT_URL_MATCH=checkout`.

---

## 1. Architecture

```
Campaign Monitor "NSR" ─ingest─► superage.awe_waitlist ┐
                                                        │
Circle sync ──────────► superage.awe_course_members (is_superage=false = real) │
Checkout ─────────────► superage.awe_course_checkout_landing_events (+ static GA)
Email clicks (SA/AH/HB/Ageist) ──daily matview─► superage.mv_awe_clicks
purchaser attribution ──────► superage.mv_awe_purchaser_acquisition
subscriber_quiz ────────────────────────────────────────┤
                                                        ▼
                                             awe_metrics Lambda (2×/day)
                                                        │  assembles ONE JSON
                                                        ▼
                          Cloudflare R2 (awe-course/awe_course.json) ─► Worker ─► index.html
```

- **Clicks** are pre-aggregated daily into the `mv_awe_clicks` matview (from the
  small click-only tables), so the Lambda never scans the big raw tables.
  **Buyer acquisition** is pre-computed daily into `mv_awe_purchaser_acquisition`
  (last-touch-before-purchase). The Lambda reads these; if one is missing it
  falls back to scanning the raw tables.

---

## 2. Data sources

| Object | Role | Key columns |
| --- | --- | --- |
| `superage.awe_waitlist` | Waitlist (Campaign Monitor "NSR"), full refresh | email, date_subscribed, utm_source/medium/campaign, sub_level, oid, custom_fields |
| `superage.awe_course_members` | Community members (Circle, $99). **Real = `is_superage = false`** (`true` = ~7 internal/team accounts, excluded everywhere) | email, **oid**, circle_created_at (purchase time), access_type, status, **is_superage** |
| `superage.awe_course_checkout_landing_events` | Course main-page landings **and** checkout events (synced from DynamoDB `email_logs_superage`). Metrics split them by **`product_url`**: `~awecourse` = course landing, `~checkout` = checkout event. Checkout events classified into acquisition buckets by UTM | **oid**, utm_source/medium/campaign, **o_event**, **product_url**, **date** (click time) |
| `superage.subscriber_quiz` | Persona (longevity quiz) | email, oid, age, gender, financial_situation, education_level, sleep_hours, exercise_freq*, smoking_status, alcohol_freq, stress_impact, created_at |
| `superage."Campaigns_Clicks"` | SuperAge email clicks | `"URL"`, `"EmailAddress "`, issue_name, `"Date"` |
| `optimism.healthbrief_clicks` | HealthBrief clicks (click-only table) | data, email, mailing_name, `"timestamp"`, type, bot |
| `optimism.allhealthy_clicks` | AllHealthy clicks (click-only table) | data, email, mailing_name, `"timestamp"`, type, bot |
| `ageist.ageist_clicks` | Ageist clicks (pre-aggregated per member/link) | email_address, campaign_title, final_url, first_seen_at, **click_count** |

---

## 2A. Data provenance — LIVE vs STATIC (read this)

**Almost everything is LIVE** (queried from RDS on every Lambda run). The **only**
hand-entered/static numbers in the whole dashboard are the **checkout-landing
historical GA counts** (420 total — see §5.10), which cover the launch window
*before* the checkout table existed. Nothing else is manual.

> **Community Members are LIVE, not static.** All members come straight from
> `superage.awe_course_members` (Circle sync, `is_superage = false`). A member who
> does **not** appear in the checkout table is still a real, live member — checkout
> tracking simply started late and never recorded their landing. Those members are
> **not** dropped and **not** hard-coded: they are counted live and attributed by
> their own acquisition UTM (null → SuperAge). "Not in checkout" ≠ "static".

| Dashboard element | Source | Live / Static |
| --- | --- | --- |
| Clicks (unique) · Total Clicks · Clicks-by-Brand · Click Trend · by-campaign | `superage.mv_awe_clicks` (4 brands) | **LIVE** |
| Waitlist total · growth · UTM acquisition | `superage.awe_waitlist` (state=Active) | **LIVE** |
| Community Members total · growth · From-Waitlist crossover | `superage.awe_course_members` (is_superage=false) | **LIVE** |
| Est. Revenue | `99 × live members_total` | **DERIVED** (live) |
| Converted-to-members / `attributed` (funnel subtext) | `superage.mv_awe_purchaser_acquisition` | **LIVE** |
| Purchaser acquisition breakdown (Acquisition tab) | `superage.mv_awe_purchaser_acquisition` | **LIVE** |
| Persona attributes · age · quiz uptake | `superage.subscriber_quiz` | **LIVE** |
| Landed via Google Ads · Google Ads funnel box · click-trend Google Ads series | live `awe_course_checkout_landing_events` (`product_url ~ awecourse`) | **LIVE** (non-unique, no oid) |
| "SuperAge Google Ads" checkout bar | live rows (`product_url ~ checkout` AND `utm_campaign=google_ads_awe`) | **LIVE** |
| **Checkout Events** — total, by-bucket, by-day (funnel + KPI + both checkout charts) | live `awe_course_checkout_landing_events` (`product_url ~ checkout`) **+ static GA history** | **HYBRID** ⚠️ |

The single **HYBRID** row is the checkout metric: `checkout.total = live checkout
events (all `product_url ~ checkout` rows) + Σ static GA counts`; `checkout.distinct`
= distinct oid among those live rows. The static portion is the
`AWE_CHECKOUT_STATIC` list in [`lambda/awe_metrics_lambda.py`](lambda/awe_metrics_lambda.py)
(also tabulated in §5.10). To change it, edit that list and redeploy the Lambda —
there is no DB row to update. When live tracking has fully caught up you can delete
the static entries and the metric becomes 100% live with no other change.

---

## 3. DB objects to create (in order)

```bash
psql "$DATABASE_URL" -f sql/awe_tables.sql                        # superage.awe_waitlist
psql "$DATABASE_URL" -f sql/awe_clicks_matview.sql               # superage.mv_awe_clicks + daily cron
# after awe_course_members + awe_course_checkout_landing_events exist:
psql "$DATABASE_URL" -f sql/awe_purchaser_acquisition_matview.sql # superage.mv_awe_purchaser_acquisition + daily cron
# optional, only if a scan is ever slow:
psql "$DATABASE_URL" -f sql/awe_indexes.sql                      # partial indexes
```

### 3a. `superage.mv_awe_clicks` (clicks matview)

One row per `(brand, email, campaign, click_date)` with `click_count`, unioning
**four brands**: SuperAge + HealthBrief + AllHealthy + Ageist AWE clicks. Sources
are the **small click-only tables** with per-source floors (SuperAge & Ageist from
2026-07-01, AH/HB from 2026-07-27) — so the build is fast.

Weighting: SuperAge/HealthBrief/AllHealthy have one row per click event (`w=1`);
**Ageist** rows are pre-aggregated per member/link, so `w = click_count`. The
rollup keeps `click_count = SUM(w)`, so non-unique totals are correct for every
brand, and `unique_clickers = COUNT(DISTINCT email) FILTER (WHERE email <> '')`.

> **Adding Ageist to an already-deployed matview:** `CREATE MATERIALIZED VIEW IF
> NOT EXISTS` is a **no-op** on an existing view, so the new Ageist `UNION` will
> NOT appear until you **drop and recreate** it (that's why Ageist unique clickers
> read 0 while its static checkout count showed 7):
> ```sql
> DROP MATERIALIZED VIEW IF EXISTS superage.mv_awe_clicks CASCADE;
> \i sql/awe_clicks_matview.sql   -- recreates + reschedules the cron
> ```
> The Lambda reads this matview first; its raw-scan fallback also handles Ageist
> now (source key `ag`, in the default `AWE_CLICK_SOURCES=sa,hb,ah,ag`).

Refreshed daily **12:30 UTC** via `REFRESH MATERIALIZED VIEW CONCURRENTLY`
(pg_cron job `refresh-mv-awe-clicks`). Full DDL in
[`sql/awe_clicks_matview.sql`](sql/awe_clicks_matview.sql).

### 3b. `superage.mv_awe_purchaser_acquisition` (last-touch-before-purchase)

One row per **real** buyer (`is_superage = false`) with the attributed UTM +
`attributed` flag. Attribution = latest checkout-landing click with `date <=
circle_created_at` (clicks after purchase excluded). A buyer with no qualifying
click, or a null/blank UTM, is attributed as **superage / email** (a SuperAge
campaign) — **not** `Unknown`. Full DDL in
[`sql/awe_purchaser_acquisition_matview.sql`](sql/awe_purchaser_acquisition_matview.sql).
Refreshed daily 12:30 UTC.

---

## 4. Lambdas

| Lambda | Trigger | What it does |
| --- | --- | --- |
| `awe_waitlist_ingest` | EventBridge ~06:00 & 18:00 ET | Pulls the CM "NSR" list, `TRUNCATE` + bulk insert into `superage.awe_waitlist` (full refresh, dedup by email). |
| `awe_checkout_landing_ingest` | EventBridge (frequent) | Incremental DynamoDB→RDS sync of `email_logs_superage` rows where `o_event = awe_course_checkout_redirect` **OR** `utm_campaign = google_ads_awe` into `superage.awe_course_checkout_landing_events`. SSM-tracked (`/awe_landing_sync/last_run`). [`lambda/awe_checkout_landing_ingest_lambda.py`](lambda/awe_checkout_landing_ingest_lambda.py). |
| `awe_metrics` | EventBridge ~06:10 & 18:10 ET | Runs all queries below, assembles one JSON, uploads to R2 `awe-course/awe_course.json`. |

**Key env vars** (full list in [README.md](README.md)):
`DB_SECRET_ARN`, `R2_SECRET_ARN`, `SA_SCHEMA=superage`, `AWE_SINCE=2026-07-01`,
`AWE_URL_PATTERNS=%superage.com/awecourse%`, `AWE_PRICE_USD=99`,
`AWE_CLICKS_MATVIEW`, `AWE_PURCHASER_MATVIEW`, `AWE_MEMBERS_TABLE`,
`AWE_LANDING_TABLE`, `SNS_TOPIC_ARN` (failure alerts → `superage-lambda-alerts`).
CM creds are plain env vars: `CM_API_KEY`, `CM_CLIENT_ID`, `CM_LIST_ID`.

Both wrap their body in try/except → publish to SNS on failure → re-raise.

---

## 5. Metric dictionary — verification SQL per KPI

Run these directly against RDS; each should match the corresponding dashboard
number. `mv_awe_clicks` is already floored per source, so queries against it need
no extra date filter. `email = ''` in `mv_awe_clicks` means "no email on the
source row" and is excluded from unique-clicker counts.

### 5.1 Overview KPIs

The KPI row (and the whole Overview) is driven by the selected brand via
`by_brand[<brand>]`; the "All" values below are the default. `kpis.*` mirrors
`by_brand.All`. Per-brand values use the same queries with the brand's `WHERE`
clause added (`mv_awe_clicks.brand`, `utm_source→brand`). Both the **Clicks** and
**Checkout Events** tiles show a big **total (non-unique)** number with a smaller
**distinct** number below it (unique clickers / distinct oid) — same pattern in the
funnel boxes.

> Display labels (what the CEO sees) vs data keys: tile **"Clicks"** =
> `total_clicks` big / `unique_clickers` below; tile **"Checkout Events"** =
> `checkout.total` (events) big / `checkout.distinct` below (the UI labels this
> **"distinct emails"**; it is deduped by `oid`, the per-person key — checkout rows
> usually have no email — so "distinct emails" == distinct people);
> tile **"Community Members"** = `members_total`. Data keys below are unchanged.

**Clicks** (tile label; `by_brand.All.unique_clickers` / `kpis.distinct_clickers`) —
distinct people who landed on the course main page through campaigns, deduped
across brands (NOT total clicks):
```sql
SELECT COUNT(DISTINCT email) FILTER (WHERE email <> '') AS unique_clickers
FROM superage.mv_awe_clicks;                       -- add: WHERE brand = 'SuperAge' for a single brand
```

**Total (non-unique) Clicks** (`kpis.total_clicks`):
```sql
SELECT COALESCE(SUM(click_count), 0) AS total_clicks
FROM superage.mv_awe_clicks;
```

**Waitlist total** (`kpis.waitlist_total`) — **ACTIVE only**:
```sql
SELECT COUNT(*) AS waitlist_total
FROM superage.awe_waitlist
WHERE state ILIKE 'active';
```

**Checkout Events** (tile label; big = `kpis.landing_events` = `checkout.total`,
small = `checkout.distinct`) — **total checkout events** (non-unique, fully additive
across buckets/brands/days) as the primary number, with **distinct oid** shown below
it (the same big/small pattern as the Clicks tile). Events = all checkout rows
(`product_url ~ checkout`) + the **static historical GA counts** (§5.10); distinct =
distinct oid among those rows (static/GA and null-oid rows have no oid, so they count
toward events but not distinct).
```sql
-- big number (events) = tracked checkout rows + Σ static; small = distinct oid:
SELECT COUNT(*)                                           AS checkout_events,   -- + Σ static
       COUNT(DISTINCT NULLIF(LOWER(TRIM(oid::text)),''))  AS checkout_distinct
FROM superage.awe_course_checkout_landing_events
WHERE LOWER(product_url) LIKE '%checkout%';   -- checkout events only (excl. course landings)
-- checkout.total = checkout_events + SUM(static counts);  checkout.distinct = checkout_distinct
```

**Landed via Google Ads** (`kpis.google_ads_landings` / `by_brand[b].google_ads_landings`)
— non-unique **course main-page** landings (no oid, so events not people), identified
by `product_url` (counted regardless of utm_campaign). Shown as its own KPI tile + a
second box beside Clicks in the funnel + a stacked series in the Click Trend.
SuperAge-only:
```sql
SELECT COUNT(*) AS google_ads_landings
FROM superage.awe_course_checkout_landing_events
WHERE LOWER(product_url) LIKE '%awecourse%';
```

**Converted to Members** (`kpis.converted_buyers`) — real buyers with a landing
click at/before purchase:
```sql
SELECT COUNT(*) FILTER (WHERE attributed) AS converted_buyers
FROM superage.mv_awe_purchaser_acquisition;   -- matview already excludes is_superage=true
```

**Community Members total** (`kpis.buyers_total`) — **real members only**:
```sql
SELECT COUNT(DISTINCT LOWER(TRIM(email))) AS members_total
FROM superage.awe_course_members
WHERE email IS NOT NULL AND TRIM(email) <> '' AND is_superage IS NOT TRUE;
```

**Est. Revenue** (`kpis.revenue_usd`) = `99 × members_total`. Kept in the payload
but **no longer shown as a KPI tile** (removed per request).

**Rates** (all derived; verify against the counts above):
```sql
-- click_to_waitlist_pct = 100 * waitlist_total / unique_clickers
-- click_to_checkout_pct = 100 * checkout.total / unique_clickers      (funnel: clicks→checkout)
-- landing_to_buyer_pct  = 100 * converted_buyers / checkout.total     (funnel: checkout→members)
```

### 5.2 Conversion funnel (full width) + per-brand

Connected: **Clicks** (top) → **Waitlist** and → **Checkout Events → Community Members**.
The top row has **two boxes side by side**: **Clicks** (big number = `total_clicks`,
small line below = `unique_clickers`) and **Google Ads** (`google_ads_landings`,
non-unique), both top-of-funnel feeding the same flow. Checkout landings include the
Google Ads checkout redirects, so Google Ads is "combined into the flow" at checkout.
The downstream conversion %s (→ Waitlist, → Checkout) are based on **unique clickers**.
Rendered full width, then two horizontal bar charts below it (§5.3, §5.9). Each brand
in the filter has its own `by_brand[<brand>].funnel` (`funnel.top` = `{count: total,
unique}`, `funnel.google_ads = {count}`):
- top = Clicks (5.1) for the brand; big=total clicks, small=unique clickers; % base = unique
- waitlist branch = Waitlist total (Active); `pct_of_top = waitlist/clickers`
- checkout branch = **Checkout Events** — `funnel.landing.count` = `checkout.total`
  (total events, big) with `funnel.landing.distinct` (distinct emails) as the small
  line. `pct_of_top = checkout events / unique clickers`.
  - **Organic / Direct side box:** `funnel.landing.organic` (the `SuperAge Website /
    Organic` bucket) renders as a separate dashed box that arrows **into** the
    Checkout node from the side — those events did NOT come from Clicks or Google Ads,
    so they're shown entering at checkout rather than flowing from the top boxes.
    Organic is still part of the `checkout.total` (the side box just makes its origin
    explicit); the box only appears when organic > 0 (SuperAge / All).
- members = **ALL** real members (`members_total`), NOT just the checkout-matched
  subset. Checkout tracking started partway through launch, so most members have no
  checkout click; they are still shown and attributed by their acquisition
  (null → SuperAge). The checkout-matched subset is carried as `buyers.attributed`
  (shown as the node's subtext) and `pct_of_landing = members_total / checkout`.

**Waitlisters who also became members** (crossover pill) — Active waitlist + real members:
```sql
SELECT COUNT(DISTINCT LOWER(TRIM(m.email))) AS waitlist_members
FROM superage.awe_course_members m
JOIN superage.awe_waitlist w
  ON LOWER(TRIM(m.email)) = LOWER(TRIM(w.email))
WHERE m.email IS NOT NULL AND TRIM(m.email) <> ''
  AND m.is_superage IS NOT TRUE AND w.state ILIKE 'active';
-- pct = 100 * waitlist_members / waitlist_total
```

### 5.3 Clicks (Overview + Acquisition charts)

**By brand** (unique clickers vs non-unique clicks):
```sql
SELECT brand,
       SUM(click_count)                                   AS clicks,
       COUNT(DISTINCT email) FILTER (WHERE email <> '')   AS unique_clickers
FROM superage.mv_awe_clicks
GROUP BY brand ORDER BY clicks DESC;
```

**By campaign** (top campaigns table):
```sql
SELECT brand, campaign, SUM(click_count) AS clicks
FROM superage.mv_awe_clicks
WHERE campaign <> ''
GROUP BY brand, campaign ORDER BY clicks DESC;
```

**By day** (click trend):
```sql
SELECT click_date AS day, SUM(click_count) AS clicks
FROM superage.mv_awe_clicks
GROUP BY click_date ORDER BY click_date;
```

### 5.4 Waitlist (growth + acquisition)

**Growth — by subscribe date** (never join date), **Active only**:
```sql
SELECT date_subscribed::date AS day,
       COUNT(*)                                                    AS new_in_period,
       SUM(COUNT(*)) OVER (ORDER BY date_subscribed::date)         AS cumulative
FROM superage.awe_waitlist
WHERE date_subscribed IS NOT NULL AND state ILIKE 'active'
GROUP BY 1 ORDER BY 1;
```

**Acquisition — by UTM** (Active only; null defaults: `utm_source→superage`,
`utm_medium→email`, `utm_campaign→Unknown`):
```sql
SELECT COALESCE(NULLIF(TRIM(utm_source), ''), 'superage') AS utm_source,  -- utm_medium→'email'
       COUNT(*) AS count
FROM superage.awe_waitlist
WHERE state ILIKE 'active'
GROUP BY 1 ORDER BY 2 DESC;
```

### 5.5 Community Members (Overview)

Two chips: **Community Members** (`by_brand[<brand>].members_total`) and **From
Waitlist**. (The old "Clicked the Link" chip was removed.) All queries are
restricted to real members (`is_superage IS NOT TRUE`).

**Members growth — by join date** (`circle_created_at`):
```sql
SELECT circle_created_at::date AS day,
       COUNT(DISTINCT LOWER(TRIM(email)))                                        AS new_in_period,
       SUM(COUNT(DISTINCT LOWER(TRIM(email)))) OVER (ORDER BY circle_created_at::date) AS cumulative
FROM superage.awe_course_members
WHERE circle_created_at IS NOT NULL AND email IS NOT NULL AND TRIM(email) <> ''
  AND is_superage IS NOT TRUE
GROUP BY 1 ORDER BY 1;
```

### 5.6 Member acquisition — last-touch-before-purchase (Acquisition tab: "Members")

Read straight from the matview (attribution already applied; null/blank acquisition
baked in as **superage / email**, and `is_superage = true` members excluded):
```sql
-- by UTM source (repeat for utm_medium, utm_campaign)
SELECT utm_source, COUNT(*) AS count
FROM superage.mv_awe_purchaser_acquisition
GROUP BY 1 ORDER BY 2 DESC;

-- totals
SELECT COUNT(*)                          AS buyers,       -- one row per buyer
       COUNT(*) FILTER (WHERE attributed) AS attributed   -- had a pre-purchase click
FROM superage.mv_awe_purchaser_acquisition;
```

To validate the attribution logic itself (what the matview computes):
```sql
WITH bp AS (   -- one purchase time + oid per buyer
  SELECT DISTINCT ON (LOWER(TRIM(email)))
         LOWER(TRIM(email)) AS email, LOWER(TRIM(oid::text)) AS oid, circle_created_at AS purchased_at
  FROM superage.awe_course_members
  WHERE email IS NOT NULL AND TRIM(email) <> '' AND circle_created_at IS NOT NULL
    AND NULLIF(TRIM(oid::text), '') IS NOT NULL
  ORDER BY LOWER(TRIM(email)), circle_created_at ASC
)
SELECT DISTINCT ON (bp.email) bp.email, l.utm_source, l.date AS click_date, bp.purchased_at
FROM bp
JOIN superage.awe_course_checkout_landing_events l ON LOWER(TRIM(l.oid::text)) = bp.oid
WHERE l.date IS NOT NULL AND l.date <= bp.purchased_at   -- click at/before purchase
ORDER BY bp.email, l.date DESC;                          -- latest such click wins
```

### 5.7 Persona (Audience Persona tab)

Persona = an audience joined to the deduped quiz **by email**. Define the quiz
once, then swap the `aud` audience per segment.

```sql
WITH quiz AS (   -- one quiz row per email (latest)
  SELECT DISTINCT ON (LOWER(TRIM(email)))
         LOWER(TRIM(email)) AS email, age, gender, financial_situation, education_level,
         sleep_hours, smoking_status, alcohol_freq, stress_impact,
         COALESCE(exercise_freq, exercise_freq_male, exercise_freq_female, exercise_freq_other) AS exercise_freq
  FROM superage.subscriber_quiz
  WHERE email IS NOT NULL AND TRIM(email) <> ''
  ORDER BY LOWER(TRIM(email)), created_at DESC
),
aud AS (   -- SEGMENT — pick ONE:
  -- clickers:
  SELECT DISTINCT email FROM superage.mv_awe_clicks WHERE email <> ''
  -- waitlisters:
  -- SELECT DISTINCT LOWER(TRIM(email)) AS email FROM superage.awe_waitlist WHERE email IS NOT NULL AND TRIM(email) <> ''
  -- buyers:
  -- SELECT DISTINCT LOWER(TRIM(email)) AS email FROM superage.awe_course_members WHERE email IS NOT NULL AND TRIM(email) <> ''
  -- all (union of the three above)
)
-- audience_total / matched_quiz / avg_age:
SELECT (SELECT COUNT(*) FROM aud)                                   AS audience_total,
       COUNT(*)                                                      AS matched_quiz,
       ROUND(AVG(q.age)::numeric, 1)                                 AS avg_age
FROM aud a JOIN quiz q ON a.email = q.email;
```

**Attribute distribution** (repeat for gender, financial_situation, education_level,
sleep_hours, exercise_freq, smoking_status, alcohol_freq, stress_impact — using the
same `quiz` + `aud` CTEs):
```sql
SELECT COALESCE(NULLIF(TRIM(q.gender::text), ''), 'Not specified') AS val, COUNT(*) AS count
FROM aud a JOIN quiz q ON a.email = q.email
GROUP BY 1 ORDER BY 2 DESC;
```

**Age buckets:**
```sql
SELECT CASE WHEN q.age < 45 THEN 'Under 45' WHEN q.age < 55 THEN '45-54'
            WHEN q.age < 65 THEN '55-64' WHEN q.age < 75 THEN '65-74' ELSE '75+' END AS range,
       COUNT(*) AS count
FROM aud a JOIN quiz q ON a.email = q.email
WHERE q.age IS NOT NULL
GROUP BY 1 ORDER BY MIN(q.age);
```

> Note: `marital_status`, `is_obese`, and `longevity_score` are intentionally
> NOT shown in the persona.

### 5.8 Quiz uptake (Persona tab)

Per segment: `total` = `audience_total`, `took_quiz` = `matched_quiz` (5.7),
`pct = 100 * took_quiz / total`.

### 5.9 Acquisition "All" + checkout acquisition buckets

Acquisition "All" = merge of Waitlist (5.4) + Members (5.6) — sum counts per UTM
value across both.

**Checkout acquisition buckets** (`checkout.by_bucket`, the second horizontal bar
chart on the Overview). Every landing row (and each static entry) is classified by
its `(utm_source, utm_medium)` into a bucket, and each bucket carries a `brand` used
by the page-level filter. Null/blank UTM on a checkout row defaults to
`source=superage, medium=website`. Rules (first match wins):

Only checkout rows (`product_url` contains `checkout`) are counted here — the
course main-page landings (`product_url` contains `awecourse`) are excluded (they're
the top-of-funnel metric instead). Classification (first match wins):

| Condition | Bucket | Brand |
| --- | --- | --- |
| `utm_campaign = google_ads_awe` | `SuperAge Google Ads` | SuperAge |
| `superage` + `website` | `SuperAge Website / Organic` | SuperAge |
| `{superage, allhealthy, healthbrief, ageist}` + `email` | `<Brand> Campaigns` | that brand |
| `superage` + anything else | `SuperAge Website / Organic` | SuperAge |
| known brand + anything else | `<Brand> Campaigns` | that brand |
| any other source `X` | `X` (verbatim) | Other |

Bucket / brand counts are **total events** (non-unique, additive), matching the
Checkout Events tile (§5.1):
```sql
SELECT LOWER(TRIM(utm_source)) AS src, LOWER(TRIM(utm_medium)) AS med,
       LOWER(TRIM(utm_campaign)) AS camp,
       COUNT(*) AS events
FROM superage.awe_course_checkout_landing_events
WHERE LOWER(product_url) LIKE '%checkout%'      -- checkout events only
GROUP BY 1, 2, 3;
-- classify each row into a bucket (utm_campaign=google_ads_awe -> 'SuperAge Google Ads',
-- else by (src, med)), then per bucket:  count = events + static-for-that-bucket
```

### 5.10 Static historical checkout counts (Google Analytics)

Checkout-landing tracking started only partway through launch, so the window before
the table existed is filled from **Google Analytics** and summed into the funnel +
checkout chart + KPI. These are hard-coded in the Lambda (`AWE_CHECKOUT_STATIC`) and
must be updated there if GA is re-read. SuperAge historical → **SuperAge Campaigns**;
Ageist → **Ageist Campaigns**. The live table is used for everything it contains
(the Jul-27 overlap with GA is accepted / treated as a duplicate day, per spec).

| Bucket | Day | Count |
| --- | --- | --- |
| SuperAge Campaigns | 2026-07-19 | 173 |
| SuperAge Campaigns | 2026-07-20 | 85 |
| SuperAge Campaigns | 2026-07-21 | 14 |
| SuperAge Campaigns | 2026-07-22 | 2 |
| SuperAge Campaigns | 2026-07-23 | 16 |
| SuperAge Campaigns | 2026-07-24 | 7 |
| SuperAge Campaigns | 2026-07-25 | 2 |
| SuperAge Campaigns | 2026-07-26 | 97 |
| SuperAge Campaigns | 2026-07-27 | 17 |
| Ageist Campaigns | 2026-07-23 | 7 |
| **Total** | | **420** (SuperAge 413 + Ageist 7) |

---

## 6. Dashboard structure (`index.html`)

3 tabs, single self-contained HTML (Chart.js, light/dark, fetches the R2 JSON):

- **Overview** — a page-wide **brand filter** (All / SuperAge / AllHealthy /
  HealthBrief / Ageist) that re-renders the **entire tab** from `by_brand[<brand>]`.
  **Every** element is brand-scoped — KPIs (Clicks [total, unique in sub],
  **Landed via Google Ads**, Waitlist, Checkout Events, Community Members), funnel
  (two top boxes: Clicks + Google Ads), both horizontal charts (Clicks by Brand —
  with an extra **SuperAge · Google Ads** total-only bar (landings, no unique) — and
  Checkout Events by Acquisition incl. the **SuperAge Google Ads** bar), the
  Click Trend (email clicks + **Google Ads landings stacked**), the Community Members chips
  (Community Members + From Waitlist) and both growth charts, and both trend charts
  (Click Trend + Checkout Landing Trend). To make this possible each `by_brand[b]`
  entry carries not just the scalar counts + `funnel` but its own time series:
  `clicks_by_day`, `checkout_by_day`, `members_growth`, `waitlist_growth`, and the
  `members_from_waitlist` crossover count. `by_brand.All` holds the deduped/aggregate
  versions. Member brand attribution uses the purchaser matview (utm_source → brand,
  null → SuperAge). Trends keep daily/weekly/monthly + range toolbar; "Members" is
  used consistently (no "buyers"/"purchasers"); the "Clicked the Link" chip was
  removed.
- **Audience Persona** — segment selector (All / Clickers / Waitlisters / Members),
  quiz-uptake chart, and per-attribute charts (each segment = people who did the
  action **and** took the quiz).
- **Acquisition (UTM)** — entity selector (All / Waitlist / Members), UTM
  source/medium/campaign breakdowns + Clicks-by-brand + top campaigns.

---

## 7. Local testing

```bash
cd awe-course
# UI only (no DB):
USE_SAMPLE=1 python run_local.py && python -m http.server 8080     # http://localhost:8080/index.html
# Real run against RDS:
export DB_PASSWORD=...           # host/port/db/user default in run_local.py
python run_local.py && python -m http.server 8080
```

Cross-check: the KPI numbers on the page should equal the results of the queries
in §5 run against the same DB.

---

## 8. Deploy checklist

1. `psql -f` the three SQL files (§3), confirm the two matviews populate + pg_cron jobs exist (`SELECT * FROM cron.job;`).
   - **If `mv_awe_clicks` already existed** (e.g. from an earlier 3-brand version), `CREATE … IF NOT EXISTS` is a no-op — **`DROP MATERIALIZED VIEW … CASCADE` first** (§3a), then confirm Ageist appears: `SELECT brand, COUNT(*) FROM superage.mv_awe_clicks GROUP BY 1;`.
2. Deploy both Lambdas (py3.12, psycopg2 layer), set env vars (§4) + IAM (Secrets read, SNS publish, logs) — see [README.md](README.md).
3. Fill CM env vars; run `awe_waitlist_ingest` once (verify `awe_waitlist` row count).
4. Run `awe_metrics` (dry: `WRITE_TO_R2=false`), check the returned JSON + CloudWatch KPI log line, then enable upload.
5. Add `awe-course/awe_course.json` to the Worker `ALLOWED_KEYS` + the dashboard origin to `ALLOWED_ORIGINS`; `wrangler deploy`.
6. Schedule EventBridge (ingest → metrics, twice daily).

### 8a. Ongoing maintenance / tracking

- **Adding or changing a click brand / matview definition:** editing
  `sql/*_matview.sql` alone does nothing to a deployed view — always
  `DROP MATERIALIZED VIEW … CASCADE` then re-run the file (see §3a). `REFRESH` only
  re-runs the existing query.
- **Updating the static checkout GA numbers (§5.10):** the only manual data in the
  dashboard. Edit `AWE_CHECKOUT_STATIC` in `lambda/awe_metrics_lambda.py`, keep the
  §5.10 table in sync, and redeploy the Lambda. When live tracking has caught up,
  empty the list to make the checkout metric 100% live.
- **Turning off a click source** (e.g. before a brand's promo launches): set env
  `AWE_CLICK_SOURCES` (subset of `sa,hb,ah,ag`) — fallback scan only; the matview
  always unions all four.
- **Where each number comes from:** see the provenance table in §2A (LIVE vs
  STATIC) and the per-KPI SQL in §5 — every dashboard value is traceable to one
  query or the static list.
