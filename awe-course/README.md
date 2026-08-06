# AWE Course Dashboard

Tracks the AWE course funnel end to end: **campaign clicks → waitlist → buyers**,
plus the marketing team's priority — the **audience persona** (who clicked /
waitlisted / took the longevity quiz).

The AWE course link is:

```
https://superage.com/awecourse/?utm_source=...&utm_medium=email&utm_campaign=...&oid=...
```

matched everywhere via `AWE_URL_PATTERNS` (default `%superage.com/awecourse%`).

```
Campaign Monitor "NSR" ──► awe_waitlist_ingest ──► superage.awe_waitlist ┐
                                                                          ├─► awe_metrics ──► R2 ──► Worker ──► index.html
RDS clicks (SA/AH/HB) + superage.subscriber_quiz ────────────────────────┘
```

## Files

| File | What it is |
| --- | --- |
| `sql/awe_tables.sql` | DDL for `superage.awe_waitlist` (run once). |
| `sql/awe_clicks_matview.sql` | `superage.mv_awe_clicks` — clicks matview (daily `REFRESH CONCURRENTLY`). |
| `sql/awe_purchaser_acquisition_matview.sql` | `superage.mv_awe_purchaser_acquisition` — per-buyer UTM, last-touch-before-purchase (daily). |
| `sql/awe_indexes.sql` | OPTIONAL partial indexes (extra speed; not needed given the click-only tables + per-source floors). |
| `sql/awe_diagnostics.sql` | Read-only queries to inspect indexes/sizes/plans. |
| `lambda/awe_waitlist_ingest_lambda.py` | Campaign Monitor → RDS (full refresh). |
| `lambda/awe_metrics_lambda.py` | Builds the dashboard JSON → R2. |
| `index.html` | The dashboard (3 tabs: Overview, Audience Persona, Acquisition). |
| `run_local.py` | Run both lambdas locally, write `awe_course.json` for preview. |
| `awe_course.sample.json` | Sample payload / JSON contract for offline preview. |

## Data sources

| Brand | Table (main DB) | URL match | email | campaign | date |
| --- | --- | --- | --- | --- | --- |
| SuperAge | `superage."Campaigns_Clicks"` | `"URL"` | `"EmailAddress "` | `issue_name` | `"Date"` |
| HealthBrief | `optimism.healthbrief_clicks` | `data` | `email` | `mailing_name` | `timestamp` |
| AllHealthy | `optimism.allhealthy_clicks` | `data` | `email` | `mailing_name` | `timestamp` |

- AllHealthy / HealthBrief promos for AWE start later — those sources simply read
  **0** until then (each source is existence-guarded, so a missing/renamed table
  is skipped, never fatal).
- Persona joins the audience to `superage.subscriber_quiz` **by email**. The
  **Persona** tab selector is All / Clickers / Waitlisters / Buyers.
- **Acquisition (UTM)** is only about sign-up entities — its selector is **All /
  Waitlist / Purchasers** (no clickers). Waitlist UTMs come from the CM custom
  fields on `awe_waitlist`; **buyer UTMs come from joining `awe_course_members.oid`
  → `awe_course_checkout_landing_events.oid`**, using **last-touch-before-purchase**
  attribution: a buyer with several landing clicks is attributed to the *latest
  click at/before their purchase*; clicks **after** the purchase are ignored; a
  buyer with no qualifying click is **Unknown**. Each buyer is counted once.
  Uses landing `date` (click time) and members `circle_created_at` (purchase),
  both UTC. This attribution is pre-computed into `superage.mv_awe_purchaser_acquisition`
  (one row per buyer, utm + 'Unknown' + `attributed` flag); the Lambda reads that
  matview instead of joining. "All" merges waitlist + buyers.
- **Overview** shows the buyer path as **Checkout Landing Events → Converted to
  Buyers** (landers who purchased *after* landing) — KPIs + a landing→buyer funnel —
  rather than campaign-clicks→buyer.
- **Buyers = AWE course members** synced from Circle into `superage.awe_course_members`
  (one buyer = one distinct member email). The metrics lambda counts members,
  breaks them down by access type / status, tracks join-date growth, computes
  crossover with the waitlist and clickers, estimates revenue ($99 × members),
  and adds a **Buyers** persona segment (members joined to the quiz). Point it
  elsewhere with `AWE_MEMBERS_TABLE` if the table lives in another schema.

---

## 1. Create the DB objects (once)

```bash
psql "$DATABASE_URL" -f sql/awe_tables.sql                        # superage.awe_waitlist
psql "$DATABASE_URL" -f sql/awe_clicks_matview.sql               # superage.mv_awe_clicks + daily pg_cron
# once awe_course_members + awe_course_checkout_landing_events exist:
psql "$DATABASE_URL" -f sql/awe_purchaser_acquisition_matview.sql # per-buyer UTM (last-touch-before-purchase)
```
`mv_awe_clicks` is the clicks source; the Lambda reads it automatically. It reads
from the small click-only tables with per-source floors, so the daily
`REFRESH CONCURRENTLY` is fast and needs no extra index. `sql/awe_indexes.sql` is
OPTIONAL (only if you later want to speed things up further).

## 2. Environment variables

### `awe_waitlist_ingest`
| Var | Required | Notes |
| --- | --- | --- |
| `CM_API_KEY` | ✅ | Campaign Monitor API key (Basic-auth username) |
| `CM_LIST_ID` | ✅ | CM list id for the "NSR" waitlist |
| `CM_CLIENT_ID` | – | informational / logging only |
| `CM_STATES` | – | default `active,unsubscribed` |
| `DB_SECRET_ARN` | ✅ | Secrets Manager secret (host/port/dbname/username/password) |
| `SA_SCHEMA` | – | default `superage` |
| `SNS_TOPIC_ARN` | – | `arn:aws:sns:us-west-1:550130133458:superage-lambda-alerts` |
| `DRY_RUN` | – | `true` = fetch + log only |

### `awe_metrics`
| Var | Required | Notes |
| --- | --- | --- |
| `R2_SECRET_ARN` | ✅ | secret: `account_id`/`access_key_id`/`secret_access_key`/`bucket_name` |
| `R2_FILE_PATH` | – | default `awe-course/awe_course.json` |
| `WRITE_TO_R2` | – | `false` = dry run |
| `DB_SECRET_ARN` | ✅ | main-db secret |
| `SA_SCHEMA` | – | default `superage` |
| `AWE_URL_PATTERNS` | – | default `%superage.com/awecourse%` (comma list) |
| `AWE_CLICKS_MATVIEW` | – | clicks matview, default `superage.mv_awe_clicks`. When it exists the Lambda reads it (no raw scan); otherwise it falls back to scanning the raw click tables. |
| `AWE_CLICK_SOURCES` | – | optional manual lever for the raw fallback only, default `sa,hb,ah`. Narrow it to skip a source. |
| `AWE_SINCE` | – | date floor when reading the matview / raw fallback, default `2026-07-01` (earliest across sources — must not clip SuperAge). Set `""` to disable. |
| `AWE_MEMBERS_TABLE` | – | buyers source = Circle course members, default `superage.awe_course_members`. Placeholder if missing. |
| `AWE_LANDING_TABLE` | – | landing-events table (oid + utm_*) for buyer acquisition, default `superage.awe_course_checkout_landing_events`. Buyer UTMs light up once it exists. |
| `AWE_LANDING_TS_COL` | – | click-timestamp column on the landing table (auto-detected — `date` — if unset). |
| `AWE_PURCHASE_COL` | – | purchase-timestamp column on the members table (auto-detected — `circle_created_at` — if unset). |
| `AWE_PURCHASER_MATVIEW` | – | pre-computed purchaser-acquisition matview, default `superage.mv_awe_purchaser_acquisition`. Read instead of joining when present. |
| `AWE_PRICE_USD` | – | lifetime price for revenue estimate, default `99` |
| `SNS_TOPIC_ARN` | – | same alerts topic |

> Campaign Monitor creds are **plain env vars** (per request) — not Secrets Manager.

## 3. Package & deploy the lambdas

Both are Python 3.12 and depend on `boto3` (in the runtime) + `psycopg2`.
Use a psycopg2 layer (or bundle `psycopg2-binary`). No `requests` — the ingest
lambda uses stdlib `urllib` for the CM API.

```bash
# example: metrics lambda
cd lambda
zip awe_metrics.zip awe_metrics_lambda.py
aws lambda create-function \
  --function-name awe_metrics \
  --runtime python3.12 --handler awe_metrics_lambda.lambda_handler \
  --timeout 120 --memory-size 512 \
  --role arn:aws:iam::550130133458:role/<lambda-role> \
  --layers <psycopg2-layer-arn> \
  --zip-file fileb://awe_metrics.zip
# repeat for awe_waitlist_ingest (handler: awe_waitlist_ingest_lambda.lambda_handler)
```

## 4. IAM policy (attach to the lambda execution role)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Secrets",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": [
        "arn:aws:secretsmanager:us-west-1:550130133458:secret:<DB_SECRET>*",
        "arn:aws:secretsmanager:us-west-1:550130133458:secret:<R2_SECRET>*"
      ]
    },
    {
      "Sid": "FailureAlerts",
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:us-west-1:550130133458:superage-lambda-alerts"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-west-1:550130133458:*"
    }
  ]
}
```

(R2 is reached over HTTPS with keys from the R2 secret — no AWS S3 IAM needed.)

## 5. Schedule (twice daily, ingest before metrics)

EventBridge Scheduler, `America/New_York`. Run ingest, then metrics ~10 min later
so the waitlist table is fresh when metrics reads it.

```bash
# 06:00 and 18:00 ET — ingest
aws scheduler create-schedule --name awe-waitlist-ingest-am \
  --schedule-expression "cron(0 6 * * ? *)" \
  --schedule-expression-timezone "America/New_York" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target '{"Arn":"arn:aws:lambda:us-west-1:550130133458:function:awe_waitlist_ingest","RoleArn":"arn:aws:iam::550130133458:role/<scheduler-role>"}'
# 06:10 and 18:10 ET — metrics  (cron(10 6,18 * * ? *))
```

Failures publish to **`superage-lambda-alerts`** (email endpoint `o@kiwi-lytics.com`
is already subscribed), so a broken run pages the same channel as the other dashboards.

## 6. Cloudflare Worker

Already updated in `../cloudflare-worker/worker.js` — `awe-course/awe_course.json`
is in `ALLOWED_KEYS`. Before the browser can fetch it, add the dashboard's public
origin to `ALLOWED_ORIGINS` and redeploy:

```bash
cd ../cloudflare-worker && wrangler deploy
```

Public URL: `https://dashboard.pardon-ventures-06b.workers.dev/awe-course/awe_course.json`

## 7. Local test / preview (`run_local.py`)

`run_local.py` **is** the local test harness. It runs `awe_metrics_lambda`
(patching out Secrets Manager + R2), writes `awe_course.json` next to
`index.html`, and that file is exactly what the dashboard fetches when served
from `localhost`. Two modes:

**A) Real run against RDS** (tests the actual metrics lambda + SQL):
```bash
export DB_HOST=... DB_NAME=postgres DB_USER=postgres DB_PASSWORD=... SA_SCHEMA=superage
# optional: also refresh the waitlist from Campaign Monitor first
export INGEST=1 CM_API_KEY=... CM_LIST_ID=... CM_CLIENT_ID=...

python run_local.py            # -> writes awe_course.json (prints KPIs)
python -m http.server 8080
# open http://localhost:8080/index.html
```

**B) Offline UI preview** (no DB — generates `awe_course.json` from the sample):
```bash
USE_SAMPLE=1 python run_local.py
python -m http.server 8080
# open http://localhost:8080/index.html
```

`awe_course.json` is generated (git-ignored intent) — only `awe_course.sample.json`
is committed. Regenerate anytime with either mode above.

---

## Performance / timeouts

The AWE match is a leading-wildcard `LIKE`, which can't use a b-tree index, so the
cost is proportional to how many rows the source scan has to examine.

**Two things keep it fast:**
1. **Click-only source tables.** HealthBrief/AllHealthy clicks come from the
   dedicated `optimism.healthbrief_clicks` / `optimism.allhealthy_clicks` tables
   (clicks only) instead of the full `*_contact_activity` tables — a fraction of
   the rows, so the scan is quick.
2. **Per-source date floors** (loss-free — AWE clicks can't predate launch):
   SuperAge from `2026-07-01` (small; keep history), HealthBrief/AllHealthy from
   `2026-07-27`. These bound the scan via the existing date indexes.

**Architecture: the clicks matview (`sql/awe_clicks_matview.sql`).** It
pre-aggregates AWE clicks into a tiny `superage.mv_awe_clicks`
(brand, email, campaign, click_date, click_count), refreshed daily 12:30 UTC via
`REFRESH MATERIALIZED VIEW CONCURRENTLY` (pg_cron). The Lambda reads the matview
(`AWE_CLICKS_MATVIEW`) instead of scanning raw tables — so the twice-daily metrics
run is fast and never risks Lambda's 15-min cap.

**`sql/awe_indexes.sql` is OPTIONAL** — partial indexes that would make the scan
even faster, if you ever want them. Not required given the date floor. These are tiny (≈0 rows today),
built once with `CREATE INDEX CONCURRENTLY` (no write lock), and make the metrics
query effectively instant afterward.

How it works with the code:
- The lambda inlines `AWE_URL_PATTERNS` as a **validated literal** (`data ILIKE
  '%superage.com/awecourse%'`), not a bind parameter, so the planner can match
  the partial index. (Inlined values are allowlist-checked — no injection.)
- **The index predicate must equal `AWE_URL_PATTERNS`.** If you change that env
  var, rebuild the indexes with the new literal, or the planner falls back to a
  full scan. Keep it to a single stable pattern (or add one partial index per
  pattern).
- pg_trgm (installed) is a documented alternative in the same file for the case
  where the pattern must vary — bigger index, but parameter-friendly.

Also set the lambda to `timeout=900s`, `memory=1024MB+` (covers the pre-index
runs and the one-time index build window). `AWE_CLICK_SOURCES` stays `sa,hb,ah`.

## Open items before go-live

1. Fill the CM env vars (`CM_API_KEY`, `CM_CLIENT_ID`, `CM_LIST_ID`) for the "NSR" list.
2. Confirm `optimism.allhealthy_clicks` exists and that `awecourse`
   appears in the AH/HB `data` column once promos start (verify on first local run).
3. Add the dashboard's hosting origin to the Worker `ALLOWED_ORIGINS`.
4. Confirm the Circle members table is at `superage.awe_course_members` (else set
   `AWE_MEMBERS_TABLE`). Buyers/persona/revenue are already wired to it.
