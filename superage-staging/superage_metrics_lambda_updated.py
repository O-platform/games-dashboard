"""
SuperAge Dashboard — superage-metrics.json Refresh Lambda
=========================================================
Queries SuperAge dashboard tables, computes metrics, and commits
superage-staging/superage-metrics.json to GitHub.

Changes in this version:
  - Campaigns filter: requires Recipients > 95 (removes test/small sends).
  - Acquisition quality grouped by utm_source only (o_event and sub_source removed).
  - New utm_clicks_performance section: which utm_source drives the most article
    clicks and unique clicks from subscriber_clicks joined to subscribers.
  - Column marital_status used for marital status in subscriber_quiz.
  - Revenue table uses issue_date (not invoice_month).
  - GITHUB_FILE_PATH defaults to superage-staging/superage-metrics.json.

"Active" definition (used wherever the dashboard reports an "Active" count
— send-to KPI, retention KPI, retention-by-source, cohort table, 90-day
retention by source):
    state = 'Active' AND engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')

Required env vars:
  DB_SECRET_ARN   — Secrets Manager ARN (JSON: host/port/dbname/username/password)
  GITHUB_TOKEN    — Fine-grained PAT (Contents read+write on the repo)
  GITHUB_REPO     — "O-platform/retention-dshb"
  GITHUB_BRANCH   — target branch (e.g. "main")

Optional env vars:
  DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_SSLMODE
  GITHUB_FILE_PATH  (default: superage-staging/superage-metrics.json)
  SA_SCHEMA         (default: superage)
  COMMIT_TO_GITHUB  (default: true; set false for local/test run)

Runtime: Python 3.12 | Layer: psycopg2
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_db_secret_cache = None

GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "O-platform/retention-dshb")
GITHUB_FILE_PATH = os.environ.get("GITHUB_FILE_PATH", "superage-staging/superage-metrics.json")
GITHUB_BRANCH   = os.environ.get("GITHUB_BRANCH", "main")
SA_SCHEMA       = os.environ.get("SA_SCHEMA", "superage")
COMMIT_TO_GITHUB = os.environ.get("COMMIT_TO_GITHUB", "true").strip().lower() not in {"0", "false", "no"}


# ─────────────────────────────────────────────────────────────
# GitHub helpers
# ─────────────────────────────────────────────────────────────

def _date_label() -> str:
    return date.today().strftime("%b %d, %Y").replace(" 0", " ")


def commit_to_github(content: str):
    if not COMMIT_TO_GITHUB:
        logger.info("COMMIT_TO_GITHUB=false — skipping GitHub commit.")
        return
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("GITHUB_TOKEN or GITHUB_REPO not set — skipping GitHub commit.")
        return

    api_base = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "superage-metrics-lambda",
    }

    sha = None
    try:
        req = urllib.request.Request(f"{api_base}?ref={GITHUB_BRANCH}", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            sha = data.get("sha")
            logger.info("File exists SHA=%s branch=%s", (sha or "")[:12], GITHUB_BRANCH)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info("File not found — will create on branch=%s", GITHUB_BRANCH)
        else:
            logger.error("GET failed: %s %s", e.code, e.read().decode())
            return
    except Exception as e:
        logger.error("GET error: %s", e)
        return

    payload = {
        "message": f"Update superage-metrics.json — {_date_label()}",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        req = urllib.request.Request(
            api_base,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            new_sha = result.get("content", {}).get("sha", "")[:12]
            logger.info("GitHub commit OK — SHA=%s branch=%s", new_sha, GITHUB_BRANCH)
    except urllib.error.HTTPError as e:
        logger.error("PUT failed: %s %s", e.code, e.read().decode())
    except Exception as e:
        logger.error("PUT error: %s", e)


# ─────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────

def _get_db_secret():
    global _db_secret_cache
    if _db_secret_cache is not None:
        return _db_secret_cache
    secret_arn = os.environ["DB_SECRET_ARN"]
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    response = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    _db_secret_cache = secret
    logger.info("DB secret fetched.")
    return secret


def get_connection():
    secret = _get_db_secret()
    host     = os.environ.get("DB_HOST", secret.get("host"))
    port     = int(os.environ.get("DB_PORT", secret.get("port", 5432)))
    dbname   = os.environ.get("DB_NAME", secret.get("dbname"))
    user     = os.environ.get("DB_USER", secret.get("username"))
    password = secret["password"]
    return psycopg2.connect(
        host=host, port=port, dbname=dbname, user=user, password=password,
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=30,
    )


# ─────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────

def pct(n, d, decimals=1):
    if not d:
        return "0.0%"
    return f"{round(100.0 * n / d, decimals):.{decimals}f}%"


def fmt(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def safe_int(v, default=0):
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


def color_list(n):
    colors = [
        "#4f8cff", "#34d399", "#a78bfa", "#fbbf24", "#f87171",
        "#22d3ee", "#f472b6", "#f59e0b", "#10b981", "#6366f1",
        "#ec4899", "#14b8a6", "#84cc16", "#eab308", "#ef4444",
    ]
    return (colors * ((n // len(colors)) + 1))[:n]


# ─────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    S = SA_SCHEMA
    logger.info("SuperAge metrics Lambda starting — branch=%s schema=%s", GITHUB_BRANCH, S)

    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # ─────────────────────────────────────────────────────
        # 1. Subscriber overview
        # ─────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*) AS total_all_states,
                COUNT(*) FILTER (WHERE state = 'Active')       AS active,
                COUNT(*) FILTER (WHERE state = 'Unsubscribed') AS unsubscribed,
                COUNT(*) FILTER (WHERE state = 'Bounced')      AS bounced,
                COUNT(*) FILTER (WHERE state = 'Deleted')      AS deleted,
                COUNT(*) FILTER (WHERE has_taken_longevity_quiz = true) AS quiz_takers,
                COUNT(*) FILTER (WHERE took_fitness_quiz::text = '1') AS fitness_quiz_takers,
                COUNT(*) FILTER (WHERE took_menu_quiz::text = '1') AS menu_quiz_takers,
                COUNT(*) FILTER (
                    WHERE high_engagement_60d IS NOT NULL AND high_engagement_60d::text != ''
                ) AS high_eng
            FROM {S}.subscribers
            WHERE date_joined::date < CURRENT_DATE
        """)
        sub = cur.fetchone() or {}
        total_all_states     = safe_int(sub.get("total_all_states"))  # every row regardless of state
        # Per product definition: "Total Subscribers" = the active base only (state='Active')
        total_subscribers    = safe_int(sub.get("active"))
        active_subscribers   = safe_int(sub.get("active"))  # legacy alias kept for older charts
        unsubscribed_count   = safe_int(sub.get("unsubscribed"))
        bounced_count        = safe_int(sub.get("bounced"))
        deleted_count        = safe_int(sub.get("deleted"))
        quiz_takers          = safe_int(sub.get("quiz_takers"))

        # Active (send-to) + engagement-segment split inside state='Active'.
        # The Overview donut renders five slices off this row: Send-To /
        # Zombies / Ghosts / Dormant / Other. "Other" is anything that's
        # still state='Active' but whose engagement_segment is NULL/empty
        # or some segment outside the four canonical ones.
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (
                    WHERE state = 'Active'
                      AND engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
                ) AS send_to_active,
                COUNT(*) FILTER (WHERE state = 'Active' AND engagement_segment = 'Zombies') AS zombies,
                COUNT(*) FILTER (WHERE state = 'Active' AND engagement_segment = 'Ghosts')  AS ghosts,
                COUNT(*) FILTER (WHERE state = 'Active' AND engagement_segment = 'Dormant') AS dormant,
                COUNT(*) FILTER (
                    WHERE state = 'Active'
                      AND (engagement_segment IS NULL OR engagement_segment = '')
                ) AS other_segment
            FROM {S}.subscribers
            WHERE date_joined::date < CURRENT_DATE
        """)
        _eng = cur.fetchone() or {}
        send_to_active = safe_int(_eng.get("send_to_active"))
        zombies_count  = safe_int(_eng.get("zombies"))
        ghosts_count   = safe_int(_eng.get("ghosts"))
        dormant_count  = safe_int(_eng.get("dormant"))
        # "Other" = state='Active' rows whose engagement_segment is NULL/empty
        # OR an unrecognised label. Computed as a residual against the active
        # base so any new segment we haven't named ends up here automatically.
        other_segment_count = max(
            total_subscribers - send_to_active - zombies_count - ghosts_count - dormant_count,
            0,
        )
        fitness_quiz_takers  = safe_int(sub.get("fitness_quiz_takers"))
        menu_quiz_takers     = safe_int(sub.get("menu_quiz_takers"))
        high_engagement_60d  = safe_int(sub.get("high_eng"))

        cur.execute(f"""
            SELECT COALESCE(NULLIF(state, ''), 'Unknown') AS state, COUNT(*) AS cnt
            FROM {S}.subscribers
            WHERE date_joined::date < CURRENT_DATE
            GROUP BY 1 ORDER BY 2 DESC
        """)
        state_rows = cur.fetchall()

        # Growth history table — used for Overview subscriber growth chart
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', snapshot_date), 'YYYY-MM') AS month_label,
                SUM(gained)       AS gained,
                SUM(lost)         AS lost,
                MAX(total_active) AS total_active
            FROM {S}.growth_history
            WHERE snapshot_date < CURRENT_DATE
            GROUP BY DATE_TRUNC('month', snapshot_date)
            ORDER BY 1
        """)
        growth_history_rows = cur.fetchall()

        # ─────────────────────────────────────────────────────
        # 2. Campaigns — filter: Recipients > 95
        # ─────────────────────────────────────────────────────
        camp_filter = """
            "Sent Date " IS NOT NULL
            AND "Sent Date "::date < CURRENT_DATE
            AND "Recipients" > 95
        """

        cur.execute(f"""
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
            FROM {S}."Campaigns"
            WHERE {camp_filter}
        """)
        cs = cur.fetchone() or {}
        total_campaigns    = safe_int(cs.get("n"))
        total_recipients   = safe_int(cs.get("total_recipients"))
        total_unique_opens = safe_int(cs.get("total_unique_opens"))
        total_camp_clicks  = safe_int(cs.get("total_clicks"))
        total_unsubs_camp  = safe_int(cs.get("total_unsubs"))
        total_bounced_camp = safe_int(cs.get("total_bounced"))
        avg_open_rate      = safe_float(cs.get("avg_open_rate"))
        avg_click_rate     = safe_float(cs.get("avg_click_rate"))
        best_open_rate     = safe_float(cs.get("best_open_rate"))
        best_click_rate    = safe_float(cs.get("best_click_rate"))

        cur.execute(f"""
            SELECT
                "Campaign Name", "Sent Date ", "Subject",
                "Recipients", "TotalOpened", "UniqueOpened",
                "Clicks", "Unsubscribed", "Bounced", "SpamComplaints",
                "UOpenRate", "UClickRate",
                COALESCE("URL", '') AS "URL"
            FROM {S}."Campaigns"
            WHERE {camp_filter}
            ORDER BY "Sent Date " ASC
        """)
        camp_rows = cur.fetchall()

        # ─────────────────────────────────────────────────────
        # 3. Content: articles_clicks + wordpress_articles
        # ─────────────────────────────────────────────────────
        ac_type_excl = "LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')"
        wp_filter = "(published_date IS NULL OR published_date::date < CURRENT_DATE)"

        cur.execute(f"""
            SELECT
                COUNT(*) AS placements,
                COUNT(DISTINCT COALESCE(NULLIF(url,''), article_title)) AS unique_articles,
                COALESCE(SUM(unique_clicks), 0) AS unique_clicks,
                COALESCE(SUM(non_unique_clicks), 0) AS non_unique_clicks
            FROM {S}.articles_clicks ac
        """)
        content_summary = cur.fetchone() or {}

        cur.execute(f"""
            SELECT
                article_title, issue_name, url, type,
                story_position, position_category,
                unique_clicks, non_unique_clicks
            FROM {S}.articles_clicks ac
            ORDER BY unique_clicks DESC NULLS LAST
            LIMIT 40
        """)
        article_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(SUM(unique_clicks), 0) AS n
            FROM {S}.articles_clicks ac
        """)
        total_article_clicks = safe_int((cur.fetchone() or {}).get("n"))

        # Content drill table (article-level, all metadata for client-side filtering)
        # INNER JOIN to wordpress_articles — articles_clicks also tracks sponsor
        # placements that aren't WordPress articles; those are out of scope here.
        cur.execute(f"""
            WITH ac AS (
                SELECT
                    article_title, url, issue_name, issue_date,
                    unique_clicks, non_unique_clicks,
                    story_position, position_category,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.articles_clicks
                WHERE {ac_type_excl}
            ), wa AS (
                SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''))
                    article_url,
                    COALESCE(NULLIF(TRIM(categories), ''), 'Uncategorized') AS categories,
                    COALESCE(NULLIF(TRIM(tags), ''), '') AS tags,
                    COALESCE(NULLIF(TRIM(written_by), ''), 'Unknown') AS written_by,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.wordpress_articles
                WHERE {wp_filter}
                ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''), modified_date DESC NULLS LAST
            )
            SELECT
                ac.article_title AS title,
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
            LIMIT 300
        """)
        content_drill_rows = cur.fetchall()

        # Total distinct article clickers (feeds the "Unique Clickers" KPI).
        # Earlier this query also produced the 1 / 2–5 / 6–10 / 11–20 / 20+
        # click-distribution buckets, but that chart was never wired up on the
        # dashboard so the bucket counts were dropped.
        cur.execute(f"""
            SELECT COUNT(*) AS total_clickers
            FROM {S}.subscriber_clicks
        """)
        total_article_clickers = safe_int((cur.fetchone() or {}).get("total_clickers"))

        # ─────────────────────────────────────────────────────
        # 4. Acquisition quality — utm_source only
        # ─────────────────────────────────────────────────────
        # Rebuilt to support a time-window selector (All / 30d / 60d / 90d) on
        # the Audience tab and to surface 30-day / 90-day churn rates per
        # source. Click stats are now sourced from the raw `Campaigns_Clicks`
        # events table (joined to subscribers by lowercased email) so the
        # window filter actually scopes click activity — the old query used
        # the date-less `subscriber_clicks` rollup and couldn't be windowed.

        # Canonical source-label mapping — mirror of the `utmLabel()` JS
        # function in `index.html`. Applied **before** GROUP BY so aliases
        # collapse into a single row in the rollup. KEEP THIS LIST IN SYNC
        # WITH `utmLabel()` IN index.html. Pattern matches (LIKE) handle
        # date-stamped batches like TD_CPL2_20241102 and A/B suffixes like
        # RRCPL1002525 without needing one CASE branch per variant.
        #
        # NOTE on `%%`: this f-string output is later fed into a query that
        # psycopg2 prepares with `%s` parameter binding. Any literal `%` in
        # the SQL must be doubled so psycopg2 doesn't read the LIKE-pattern
        # wildcards as positional placeholders (caused an IndexError before).
        def _canon_source(col_sql: str) -> str:
            lc = f"LOWER(TRIM({col_sql}))"
            return f"""
            CASE
                -- AllHealthy
                WHEN {lc} IN ('ahcpl1', 'allhealthy', 'allhealthy.com') THEN 'AllHealthy'
                -- TrueDemocracy: TDCPL1, TDCPL2, and every TD_CPL2_YYYYMMDD batch
                WHEN {lc} = 'tdcpl1'                                    THEN 'TrueDemocracy'
                WHEN {lc} = 'tdcpl2'                                    THEN 'TrueDemocracy'
                WHEN {lc} LIKE 'td_cpl2%%'                              THEN 'TrueDemocracy'
                -- LivingSimply: CPL1, CPL2 and the .com variant
                WHEN {lc} IN ('lscpl1', 'lscpl2', 'ls_cpl2', 'livingsimply', 'livingsimply.com') THEN 'LivingSimply'
                -- DailyPuzzle
                WHEN {lc} IN ('dpcpl1', 'dp_cpl2')                      THEN 'DailyPuzzle'
                -- HealthFirst / FitConnect
                WHEN {lc} = 'hfcpl1'                                    THEN 'HealthFirst'
                WHEN {lc} = 'fccpl1'                                    THEN 'FitConnect'
                -- Meta: facebook + instagram only (IF / IFCPL1 split out below)
                WHEN {lc} IN ('facebook', 'meta', 'fb', 'ig')           THEN 'Meta'
                -- IFCPL: IF short code + IFCPL1 batch (its own brand, not Meta)
                WHEN {lc} IN ('if', 'ifcpl1')                           THEN 'IFCPL'
                -- Taboola (LOWER handles taboola/Taboola/TABOOLA)
                WHEN {lc} = 'taboola'                                   THEN 'Taboola'
                -- HealthBrief / SuperAge Quiz
                WHEN {lc} = 'healthbrief'                               THEN 'HealthBrief'
                WHEN {lc} IN ('superagequiz', 'longevity_quiz')         THEN 'SuperAge Quiz'
                -- TheAgeist + every sample/request/etc. issue
                WHEN {lc} IN ('theageist', 'theageist001', 'ageist')    THEN 'TheAgeist'
                WHEN {lc} LIKE 'ageist_%%'                              THEN 'TheAgeist'
                WHEN {lc} LIKE 'ageistrequest%%'                        THEN 'TheAgeist'
                -- RecommendedReads (new canonical label)
                WHEN {lc} IN ('recommendedreads.com', 'rr_cpl2')        THEN 'RecommendedReads'
                WHEN {lc} LIKE 'rrcpl1%%'                               THEN 'RecommendedReads'
                -- Campaign Monitor (case-only collapse)
                WHEN {lc} = 'campaign_monitor'                          THEN 'Campaign Monitor'
                -- Welcome Flow (URL-encoded variant)
                WHEN {lc} IN ('welcome flow', 'welcome+flow')           THEN 'Welcome Flow'
                -- NNCPL family (NNCPL1 + every NN_CPL2_* batch + NN1_CPL2oneclick)
                WHEN {lc} = 'nncpl1'                                    THEN 'NNCPL'
                WHEN {lc} LIKE 'nn_cpl2%%'                              THEN 'NNCPL'
                WHEN {lc} LIKE 'nn1_cpl2%%'                             THEN 'NNCPL'
                -- ISCPL family
                WHEN {lc} IN ('is', 'iscpl1')                           THEN 'ISCPL'
                -- AI referrers (ChatGPT, Perplexity, Nbot, etc.)
                WHEN {lc} IN ('chatgpt.com', 'perplexity', 'nbot.ai')   THEN 'AI'
                -- Refind / SuperAge (kept as their own labels — note SuperAge
                -- is distinct from SuperAge Quiz above)
                WHEN {lc} = 'refind'                                    THEN 'Refind'
                WHEN {lc} = 'superage'                                  THEN 'SuperAge'
                ELSE NULLIF(TRIM({col_sql}), '')
            END
            """
        def fetch_acquisition_rows(label_expr: str, fallback_label: str,
                                   since_days=None, limit: int = 12):
            sub_filter = ""
            click_filter = ""
            if since_days is not None:
                sub_filter   = f"AND date_joined::date >= CURRENT_DATE - INTERVAL '{int(since_days)} days'"
                click_filter = f"AND cc.\"Date\"::date >= CURRENT_DATE - INTERVAL '{int(since_days)} days'"
            cur.execute(f"""
                WITH s AS (
                    SELECT
                        LOWER(TRIM(email))                  AS email,
                        COALESCE({label_expr}, %s)          AS label,
                        state,
                        date_joined::date                   AS joined,
                        date_unsubscribed::date             AS unsubbed
                    FROM {S}.subscribers
                    WHERE email IS NOT NULL AND TRIM(email) != ''
                      AND date_joined::date < CURRENT_DATE
                      {sub_filter}
                ),
                cc AS (
                    SELECT
                        LOWER(TRIM(cc."EmailAddress ")) AS email,
                        COUNT(*)                         AS clicks
                    FROM {S}."Campaigns_Clicks" cc
                    WHERE cc."Date" IS NOT NULL
                      AND cc."EmailAddress " IS NOT NULL
                      AND TRIM(cc."EmailAddress ") != ''
                      {click_filter}
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
                          AND s.unsubbed IS NOT NULL AND s.joined IS NOT NULL
                          AND (s.unsubbed - s.joined) <= 30
                    )                                               AS churned_30d,
                    COUNT(*) FILTER (
                        WHERE s.state = 'Unsubscribed'
                          AND s.unsubbed IS NOT NULL AND s.joined IS NOT NULL
                          AND (s.unsubbed - s.joined) <= 90
                    )                                               AS churned_90d
                FROM s LEFT JOIN cc ON s.email = cc.email
                GROUP BY 1
                ORDER BY subscribers DESC NULLS LAST
                LIMIT {int(limit)}
            """, (fallback_label,))
            return cur.fetchall()

        # Acquisition source label: utm_source → source → 'Organic',
        # with both columns canonicalised via _canon_source().
        _label_expr = f"{_canon_source('utm_source')}, {_canon_source('source')}"
        acquisition_utm_rows     = fetch_acquisition_rows(_label_expr, "Organic")
        acquisition_utm_rows_30  = fetch_acquisition_rows(_label_expr, "Organic", since_days=30)
        acquisition_utm_rows_60  = fetch_acquisition_rows(_label_expr, "Organic", since_days=60)
        acquisition_utm_rows_90  = fetch_acquisition_rows(_label_expr, "Organic", since_days=90)

        # ─────────────────────────────────────────────────────
        # 5. UTM source subscriber click performance
        #    Which UTM source drives the most article clicks
        #    from subscriber_clicks joined to subscribers.
        # ─────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(
                  {_canon_source('s.utm_source')},
                  {_canon_source('s.source')},
                  'Organic'
                ) AS label,
                COUNT(DISTINCT sc.email_address) AS clickers,
                COALESCE(SUM(sc.unique_clicks), 0)     AS unique_clicks,
                COALESCE(SUM(sc.non_unique_clicks), 0) AS total_clicks,
                ROUND(COALESCE(SUM(sc.unique_clicks), 0)::numeric / NULLIF(COUNT(DISTINCT sc.email_address), 0), 1) AS avg_per_clicker
            FROM {S}.subscriber_clicks sc
            JOIN {S}.subscribers s
              ON LOWER(TRIM(s.email)) = LOWER(TRIM(sc.email_address))
            GROUP BY 1
            ORDER BY unique_clicks DESC NULLS LAST
            LIMIT 12
        """)
        sub_clicks_utm_rows = cur.fetchall()

        # ─────────────────────────────────────────────────────
        # 6. Longevity quiz
        # ─────────────────────────────────────────────────────
        # `avg_score` / `max_score` / `min_score` and the score-bucket
        # distribution query that used to live here were dropped when the
        # "Audience Persona" tab stopped surfacing longevity-score visuals.
        # `WHERE longevity_score IS NOT NULL` is kept as a completed-quiz
        # gate so `quiz_count` matches the previous semantics.
        cur.execute(f"""
            SELECT
                COUNT(*)                     AS n,
                ROUND(AVG(age)::numeric, 1)  AS avg_age
            FROM {S}.subscriber_quiz
            WHERE longevity_score IS NOT NULL
        """)
        qs = cur.fetchone() or {}
        quiz_count   = safe_int(qs.get("n"))
        avg_age_quiz = safe_float(qs.get("avg_age"))

        cur.execute(f"""
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
            FROM {S}.subscriber_quiz
            WHERE age IS NOT NULL
            GROUP BY 1 ORDER BY MIN(age)
        """)
        quiz_age_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(NULLIF(gender, ''), 'Unknown') AS gender, COUNT(*) AS cnt
            FROM {S}.subscriber_quiz
            GROUP BY 1 ORDER BY 2 DESC
        """)
        quiz_gender_rows = cur.fetchall()

        cur.execute(f"""
            SELECT
                COALESCE(
                    NULLIF(exercise_freq, ''),
                    NULLIF(exercise_freq_male, ''),
                    NULLIF(exercise_freq_female, ''),
                    'Unknown'
                ) AS freq,
                COUNT(*) AS cnt
            FROM {S}.subscriber_quiz
            GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """)
        exercise_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(NULLIF(sleep_hours, ''), 'Unknown') AS sleep, COUNT(*) AS cnt
            FROM {S}.subscriber_quiz
            GROUP BY 1 ORDER BY 2 DESC
        """)
        sleep_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(NULLIF(education_level, ''), 'Unknown') AS label, COUNT(*) AS cnt
            FROM {S}.subscriber_quiz
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12
        """)
        education_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(NULLIF(marital_status, ''), 'Unknown') AS label, COUNT(*) AS cnt
            FROM {S}.subscriber_quiz
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12
        """)
        marital_rows = cur.fetchall()

        cur.execute(f"""
            SELECT
                CASE
                    WHEN is_obese IS NULL THEN 'Unknown'
                    WHEN LOWER(is_obese::text) IN ('1','true','yes','y') THEN 'Obese'
                    WHEN LOWER(is_obese::text) IN ('0','false','no','n') THEN 'Not Obese'
                    ELSE is_obese::text
                END AS label,
                COUNT(*) AS cnt
            FROM {S}.subscriber_quiz
            GROUP BY 1 ORDER BY 2 DESC
        """)
        obesity_rows = cur.fetchall()

        # ─────────────────────────────────────────────────────
        # 7. Revenue and sponsors
        # ─────────────────────────────────────────────────────
        # Every query against sa_airtable_sales requires `sponsor_type IS NOT
        # NULL AND TRIM(sponsor_type) != ''` so rows without a categorised
        # sponsor type don't pollute totals, monthly chart, top sponsors, or
        # the donut. Source-of-truth column is `$_line_amount` (Airtable's
        # line-item dollar value); the public KPI is labelled "Total Line
        # Amount" in the dashboard, but the JSON field stays `total_revenue`
        # / `total_revenue_fmt` for backwards-compat with anything reading
        # the metrics JSON directly.
        _sponsor_filter = (
            "\"$_line_amount\" IS NOT NULL AND \"$_line_amount\" != ''\n"
            "              AND sponsor_type IS NOT NULL AND TRIM(sponsor_type) != ''"
        )
        cur.execute(f"""
            SELECT
                COUNT(*) AS n,
                SUM(NULLIF("$_line_amount", '')::numeric) AS total_revenue,
                AVG(NULLIF("$_line_amount", '')::numeric) AS avg_deal,
                MAX(NULLIF("$_line_amount", '')::numeric) AS max_deal
            FROM {S}.sa_airtable_sales
            WHERE {_sponsor_filter}
        """)
        rs = cur.fetchone() or {}
        total_revenue    = safe_float(rs.get("total_revenue"))
        avg_deal_size    = safe_float(rs.get("avg_deal"))
        total_sponsor_deals = safe_int(rs.get("n"))

        cur.execute(f"""
            SELECT
                DATE_TRUNC('month', issue_date::date)::date AS month_start,
                TO_CHAR(DATE_TRUNC('month', issue_date::date), 'Mon YYYY') AS month_label,
                SUM(NULLIF("$_line_amount", '')::numeric) AS revenue,
                COUNT(*) AS deals
            FROM {S}.sa_airtable_sales
            WHERE issue_date IS NOT NULL
              AND TRIM(CAST(issue_date AS TEXT)) != ''
              AND {_sponsor_filter}
            GROUP BY 1, 2
            ORDER BY 1
        """)
        rev_monthly_rows = cur.fetchall()

        cur.execute(f"""
            SELECT
                COALESCE("sponsor_name"->>0, "sponsor_name"::text, 'Unknown') AS sponsor,
                COUNT(*) AS deals,
                SUM(NULLIF("$_line_amount", '')::numeric) AS revenue
            FROM {S}.sa_airtable_sales
            WHERE {_sponsor_filter}
            GROUP BY 1 ORDER BY 3 DESC NULLS LAST LIMIT 10
        """)
        sponsor_rows = cur.fetchall()

        cur.execute(f"""
            SELECT NULLIF(TRIM(sponsor_type), '') AS stype, COUNT(*) AS cnt
            FROM {S}.sa_airtable_sales
            WHERE sponsor_type IS NOT NULL AND TRIM(sponsor_type) != ''
            GROUP BY 1 ORDER BY 2 DESC
        """)
        sponsor_type_rows = cur.fetchall()
        # ECPM retired (2026-05): the dedicated avg-ecpm query and the
        # per-sponsor avg_ecpm column were removed from the dashboard.

        # ─────────────────────────────────────────────────────
        # 8. Retention
        # ─────────────────────────────────────────────────────
        # "Active" = state='Active' AND engagement_segment NOT IN (Ghosts/Zombies/Dormant).
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
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
            FROM {S}.subscribers
            WHERE date_joined::date < CURRENT_DATE
        """)
        retention_overall_row = cur.fetchone() or {}

        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE days_active BETWEEN 0   AND 30)  AS d0_30,
                COUNT(*) FILTER (WHERE days_active BETWEEN 31  AND 90)  AS d31_90,
                COUNT(*) FILTER (WHERE days_active BETWEEN 91  AND 180) AS d91_180,
                COUNT(*) FILTER (WHERE days_active BETWEEN 181 AND 365) AS d181_365,
                COUNT(*) FILTER (WHERE days_active > 365) AS d365plus
            FROM (
                SELECT
                    EXTRACT(EPOCH FROM (COALESCE(date_unsubscribed, NOW()) - date_joined)) / 86400 AS days_active
                FROM {S}.subscribers
                WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
                  AND (date_unsubscribed IS NULL OR date_unsubscribed::date < CURRENT_DATE)
            ) x
        """)
        lifespan_dist_row = cur.fetchone() or {}

        cur.execute(f"""
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
                FROM {S}.subscribers
                WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
                  AND (date_unsubscribed IS NULL OR date_unsubscribed::date < CURRENT_DATE)
            ) x
        """)
        survival_row = cur.fetchone() or {}

        # Survival curves split by acquisition source bucket — overlays one
        # line per source on the Retention tab so churn shapes can be
        # compared. Uses the same canonical source mapping as Q35b (Direct
        # rolls up organic / direct / empty). Minimum cohort of 100 to keep
        # one-off / test sources out of the legend; no LIMIT — the chart's
        # legend supports click-to-toggle so users can show/hide individual
        # sources, and quick "show all / hide all" buttons live above the
        # chart for bulk control.
        cur.execute(f"""
            WITH s AS (
                SELECT
                    CASE
                        WHEN LOWER(COALESCE(NULLIF(TRIM(utm_source),''),
                                            NULLIF(TRIM(source),''), '')) IN ('organic','direct','') THEN 'Direct'
                        ELSE COALESCE(
                            {_canon_source("COALESCE(NULLIF(TRIM(utm_source),''), NULLIF(TRIM(source),''))")},
                            'Direct'
                        )
                    END AS bucket,
                    EXTRACT(EPOCH FROM (date_unsubscribed - date_joined)) / 86400 AS days_to_unsub
                FROM {S}.subscribers
                WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
                  AND (date_unsubscribed IS NULL OR date_unsubscribed::date < CURRENT_DATE)
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
            GROUP BY 1
            HAVING COUNT(*) >= 100
            -- Sort by **365-day survival rate** so the dropdown lists the
            -- best-retaining sources first (top of the curve). Tie-breaker
            -- is cohort size DESC so larger sources surface over smaller
            -- ones when the rate is identical.
            ORDER BY
                (COUNT(*) FILTER (WHERE days_to_unsub IS NULL OR days_to_unsub > 365)::numeric
                 / NULLIF(COUNT(*), 0)) DESC NULLS LAST,
                total DESC
        """)
        survival_by_source_rows = cur.fetchall()

        # Monthly churn volume + two churn-rate flavours.
        #
        # The bar chart counts subscribers who unsubscribed in each month
        # (`subscribers.date_unsubscribed`). On top of that we emit two
        # different rate series so the Retention tab can show them in
        # separate visuals (no overlay):
        #
        #   • `churn_pct`           = list-churn / sends
        #                           = (subscribers.date_unsubscribed in month)
        #                           ÷ SUM(Campaigns.Recipients) in month × 100
        #     Counts ALL channels that took someone off the list (campaign
        #     unsubs, hard bounces, manual deletions), so it's the "list
        #     net-lost someone per email impression" rate.
        #
        #   • `campaign_unsub_pct`  = campaign-attributed unsubs / sends
        #                           = SUM(Campaigns.Unsubscribed) in month
        #                           ÷ SUM(Campaigns.Recipients) in month × 100
        #     Both sides from the SAME table + window. Counts only
        #     subscribers who clicked the unsub link in a campaign sent
        #     that month — the email-marketing-narrow definition.
        #
        # IMPORTANT — Recipients is NOT deduplicated across campaigns.
        # The same subscriber appears in `Recipients` once per campaign
        # they received, so SUM(Recipients) is total email **send events
        # / impressions**, not unique people reached. That's intentional:
        # we want a per-impression damage signal so a high-frequency
        # month with many campaigns isn't artificially favoured over a
        # low-frequency one. Same caveat applies to both rate variants.
        cur.execute(f"""
            WITH unsubs AS (
                SELECT
                    DATE_TRUNC('month', date_unsubscribed)::date AS month,
                    COUNT(*) AS churned
                FROM {S}.subscribers
                WHERE state = 'Unsubscribed'
                  AND date_unsubscribed IS NOT NULL
                  AND date_unsubscribed::date < CURRENT_DATE
                GROUP BY 1
            ),
            sends AS (
                SELECT
                    DATE_TRUNC('month', "Sent Date "::date)::date AS month,
                    SUM("Recipients")                              AS total_sent,
                    COUNT(*)                                       AS campaigns,
                    COALESCE(SUM("Unsubscribed"), 0)               AS campaign_unsubs
                FROM {S}."Campaigns"
                WHERE "Sent Date " IS NOT NULL
                  AND "Sent Date "::date < CURRENT_DATE
                  AND "Recipients" > 95
                GROUP BY 1
            )
            SELECT
                COALESCE(u.month, s.month)              AS month,
                COALESCE(u.churned, 0)                  AS churned,
                COALESCE(s.total_sent, 0)               AS total_sent,
                COALESCE(s.campaigns, 0)                AS campaigns,
                COALESCE(s.campaign_unsubs, 0)          AS campaign_unsubs,
                CASE
                    WHEN COALESCE(s.total_sent, 0) = 0 THEN NULL
                    ELSE ROUND(COALESCE(u.churned, 0)::numeric
                              / s.total_sent * 100, 4)
                END                                     AS churn_pct,
                CASE
                    WHEN COALESCE(s.total_sent, 0) = 0 THEN NULL
                    ELSE ROUND(COALESCE(s.campaign_unsubs, 0)::numeric
                              / s.total_sent * 100, 4)
                END                                     AS campaign_unsub_pct
            FROM unsubs u
            FULL OUTER JOIN sends s ON u.month = s.month
            ORDER BY 1
        """)
        churn_monthly_rows = cur.fetchall()

        # ─────────────────────────────────────────────────────
        # 8b. Retention by Acquisition Source
        # ─────────────────────────────────────────────────────
        # Bucket each subscriber's COALESCE(utm_source, source, 'Organic') into
        # the six product-relevant buckets, then compute LTV and early-unsub
        # rates, plus total unique article clicks via subscriber_clicks.
        # "Active Now" uses the same two-condition Active rule as Q1b / Q35.
        cur.execute(f"""
            WITH s AS (
                SELECT
                    LOWER(TRIM(email))                                                 AS email,
                    date_joined::date                                                  AS joined,
                    date_unsubscribed::date                                            AS unsubbed,
                    state,
                    engagement_segment,
                    COALESCE(NULLIF(TRIM(utm_source), ''), NULLIF(TRIM(source), ''), 'Organic') AS source_raw
                FROM {S}.subscribers
                WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
            ),
            mapped AS (
                SELECT
                    s.*,
                    -- Use the canonical source-label mapping (Source label
                    -- canonicalisation in §4 of METRICS_updated.md). Coerce
                    -- 'organic' / 'direct' / '' to 'Direct' so they share a
                    -- single bucket; everything else goes through _canon_source.
                    CASE
                        WHEN LOWER(source_raw) IN ('organic', 'direct', '') THEN 'Direct'
                        ELSE COALESCE({_canon_source('source_raw')}, 'Direct')
                    END AS bucket,
                    CASE WHEN unsubbed IS NOT NULL THEN (unsubbed - joined) END        AS lifespan_days
                FROM s
            ),
            clicks AS (
                SELECT LOWER(TRIM(email_address)) AS email,
                       SUM(unique_clicks)         AS unique_clicks
                FROM {S}.subscriber_clicks
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
            -- WHERE filter was previously restricting to the old fixed bucket
            -- list ('AH CPL', 'Ageist CPL', 'Share', 'Meta', 'Google', 'Direct').
            -- That filter dropped every canonical bucket name except 'Meta'
            -- when Q35b was refactored to use _canon_source — so the table
            -- silently shrank to one row. Replaced with a HAVING threshold
            -- so the table now lists every canonical bucket with a non-tiny
            -- cohort (min 100 subscribers), top 15 by size.
            GROUP BY m.bucket
            HAVING COUNT(*) >= 100
            ORDER BY subscribers DESC
            LIMIT 15
        """)
        retention_by_source_rows = cur.fetchall()

        # ─────────────────────────────────────────────────────
        # 9. Cohort Analysis
        # ─────────────────────────────────────────────────────

        # Retention heatmap: for each join-month cohort, % still active at month offset 1..12
        cur.execute(f"""
            WITH cohorts AS (
                SELECT
                    DATE_TRUNC('month', date_joined::date)::date AS cohort_month,
                    email,
                    date_joined::date AS joined,
                    date_unsubscribed::date AS unsubbed
                FROM {S}.subscribers
                WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
            )
            SELECT
                cohort_month,
                COUNT(*)                                                                  AS cohort_size,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined +  30) AS alive_m1,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined +  60) AS alive_m2,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined +  90) AS alive_m3,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 120) AS alive_m4,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 150) AS alive_m5,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 180) AS alive_m6,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 270) AS alive_m9,
                COUNT(*) FILTER (WHERE unsubbed IS NULL OR unsubbed > joined + 365) AS alive_m12
            FROM cohorts
            GROUP BY 1 ORDER BY 1
        """)
        cohort_heatmap_rows = cur.fetchall()

        # Overall survival curve by join-month cohort (for the overlay line chart)
        # already captured above via survival_rows; reuse for cohort level

        # Cohort performance table — restricted to 2025+ cohorts (earlier
        # months are kept in the heatmap above but dropped here so this
        # table focuses on recent acquisition quality). Three primary
        # counts per cohort, all derived from the same row scan:
        #   • total                — cohort size at join (the original cohort)
        #   • total_subscribers    — still on the list (state IN ('Active','Bounced'))
        #   • active_now           — reachable + engaged
        #                            (state='Active' AND engagement_segment NOT IN dormant)
        # Plus derived rates and the campaigns-sent count for context.
        cur.execute(f"""
            WITH cohorts AS (
                SELECT
                    TO_CHAR(DATE_TRUNC('month', date_joined::date), 'Mon YYYY') AS cohort_label,
                    DATE_TRUNC('month', date_joined::date)::date AS cohort_month,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE state IN ('Active', 'Bounced')) AS total_subscribers,
                    COUNT(*) FILTER (
                        WHERE state = 'Active'
                          AND engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
                    ) AS active_now,
                    COUNT(*) FILTER (WHERE state = 'Unsubscribed') AS churned,
                    COUNT(*) FILTER (WHERE unsubbed_within_90) AS churned_90d
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
                    FROM {S}.subscribers
                    WHERE date_joined IS NOT NULL
                      AND date_joined::date < CURRENT_DATE
                      AND date_joined::date >= DATE '2025-01-01'
                ) x
                GROUP BY 1, 2
                HAVING COUNT(*) >= 20
                ORDER BY 2
            ),
            camps AS (
                SELECT
                    DATE_TRUNC('month', "Sent Date "::date)::date AS camp_month,
                    COUNT(*) AS campaigns_sent
                FROM {S}."Campaigns"
                WHERE "Sent Date " IS NOT NULL
                  AND "Sent Date "::date < CURRENT_DATE
                  AND "Recipients" > 95
                GROUP BY 1
            )
            SELECT
                c.*,
                -- Active % is Total Active / Cohort Size (canonical Active rule:
                -- state='Active' AND engagement_segment NOT IN Ghosts/Zombies/Dormant).
                -- Replaces the older "Still on List %" (which used state IN
                -- Active+Bounced as the numerator) so the table has one
                -- engagement-quality percentage instead of two near-duplicates.
                ROUND(c.active_now::numeric / NULLIF(c.total,0) * 100, 1) AS active_pct,
                ROUND(c.churned::numeric / NULLIF(c.total,0) * 100, 1) AS churn_rate_pct,
                ROUND(c.churned_90d::numeric / NULLIF(c.total,0) * 100, 1) AS early_churn_pct,
                COALESCE(k.campaigns_sent, 0) AS campaigns_sent
            FROM cohorts c
            LEFT JOIN camps k ON k.camp_month = c.cohort_month
            ORDER BY c.cohort_month
        """)
        cohort_table_rows = cur.fetchall()

    finally:
        cur.close()
        conn.close()

    # ─────────────────────────────────────────────────────────
    # Build JSON payload
    # ─────────────────────────────────────────────────────────
    M = {}
    M["data_as_of"] = _date_label()

    # Subscriber KPIs.
    # `total_subscribers` is the **active base** (state='Active'); product
    # definition. `total_all_states` is the raw row count across all states —
    # used as the denominator for state-mix ratios (unsub % of base, bounce
    # % of base, etc.) so those percentages still sum to 100.
    M["total_subscribers"]   = total_subscribers          # = active state count
    M["send_to_active"]      = send_to_active             # state='Active' AND engagement_segment NOT IN (Ghost/Zombie/Dormant)
    M["unsubscribed_count"]  = unsubscribed_count
    M["bounced_count"]       = bounced_count
    M["quiz_takers"]         = quiz_takers
    M["quiz_takers_pct"]     = round(100.0 * quiz_takers / total_subscribers, 1) if total_subscribers else 0
    M["high_engagement_60d"] = high_engagement_60d
    M["active_rate"]         = pct(active_subscribers, total_all_states)  # active / all states
    M["send_to_rate"]        = pct(send_to_active,    total_subscribers)  # send-to / active base

    # Campaign KPIs (Recipients > 95)
    M["total_campaigns"]    = total_campaigns
    M["total_recipients"]   = total_recipients
    M["avg_open_rate"]      = f"{avg_open_rate:.2f}%"
    M["avg_click_rate"]     = f"{avg_click_rate:.2f}%"
    M["best_open_rate"]     = f"{best_open_rate:.2f}%"
    M["best_click_rate"]    = f"{best_click_rate:.2f}%"

    # Content KPIs
    M["content_summary"] = {
        "placements":      safe_int(content_summary.get("placements")),
        "unique_articles": safe_int(content_summary.get("unique_articles")),
        "unique_clicks":   safe_int(content_summary.get("unique_clicks")),
        "non_unique_clicks": safe_int(content_summary.get("non_unique_clicks")),
    }
    M["total_article_clicks"]   = total_article_clicks
    M["total_article_clickers"] = total_article_clickers

    # Revenue KPIs
    M["total_revenue"]      = total_revenue
    M["total_revenue_fmt"]  = f"${total_revenue:,.0f}"
    M["avg_deal_size_fmt"]  = f"${avg_deal_size:,.0f}"
    M["total_sponsor_deals"]= total_sponsor_deals

    # Audience-Persona KPIs (longevity-score fields were removed when the
    # tab stopped surfacing score visuals).
    M["quiz_kpis"] = {
        "total_takers":        quiz_count,
        "avg_age":             avg_age_quiz,
        "fitness_quiz_takers": fitness_quiz_takers,
        "menu_quiz_takers":    menu_quiz_takers,
    }

    # Subscriber state distribution (legacy four-state donut). Still emitted
    # for backwards-compat, but the Overview tab now renders the new
    # `subscriber_engagement_mix` donut below instead.
    state_colors = {
        "Active": "#34d399", "Unsubscribed": "#f87171",
        "Bounced": "#fbbf24", "Deleted": "#9ca3af", "Unknown": "#6b7280",
    }
    M["subscriber_states"] = {
        "labels": [r["state"] for r in state_rows],
        "data":   [safe_int(r["cnt"]) for r in state_rows],
        "colors": [state_colors.get(r["state"], "#a78bfa") for r in state_rows],
    }

    # Current Subscriber Mix donut — denominator is **current subscribers
    # only** (state='Active'), split by engagement_segment. Three slices:
    #   • Send-To  = engagement_segment NOT IN ('Ghosts','Zombies','Dormant')
    #   • Dormant / Ghost / Zombie = engagement_segment IN ('Ghosts','Zombies','Dormant')
    #     (the three are merged into a single "disengaged" bucket — see
    #     `_eng` row for the per-segment split if you ever need to break
    #     them apart again).
    #   • Other    = NULL / empty / unrecognised segment (residual so any
    #                future-added segment label rolls up here automatically)
    # Unsubscribed / Bounced / Deleted are NOT part of this view — they're
    # not in state='Active' so they don't belong on a "current subscribers"
    # breakdown.
    dormant_bucket = zombies_count + ghosts_count + dormant_count
    M["subscriber_engagement_mix"] = {
        "labels": ["Send-To", "Dormant / Ghost / Zombie", "Other"],
        "data":   [send_to_active, dormant_bucket, other_segment_count],
        "colors": ["#1a7f37", "#9a6700", "#9ca3af"],
    }

    # Overview subscriber growth chart — sourced from growth_history table
    M["subscriber_monthly"] = {
        "labels":       [str(r["month_label"]) for r in growth_history_rows],
        "new_subs":     [safe_int(r["gained"]) for r in growth_history_rows],
        "unsubs":       [safe_int(r["lost"]) for r in growth_history_rows],
        "active_count": [safe_int(r["total_active"]) for r in growth_history_rows],
    }

    # Campaign trend (last 30)
    trend_slice = camp_rows[-30:] if len(camp_rows) > 30 else camp_rows
    M["campaign_trend"] = {
        "labels":      [str(r["Campaign Name"])[:20] for r in trend_slice],
        "open_rates":  [safe_float(r["UOpenRate"]) for r in trend_slice],
        "click_rates": [safe_float(r["UClickRate"]) for r in trend_slice],
        "recipients":  [safe_int(r["Recipients"]) for r in trend_slice],
    }

    camp_table = [
        {
            "name":        str(r["Campaign Name"] or ""),
            "subject":     str(r["Subject"] or ""),
            "sent_date":   str(r["Sent Date "])[:10] if r["Sent Date "] else "",
            "recipients":  fmt(safe_int(r["Recipients"])),
            "unique_opens":fmt(safe_int(r["UniqueOpened"])),
            "open_rate":   f"{safe_float(r['UOpenRate']):.2f}%",
            "clicks":      fmt(safe_int(r["Clicks"])),
            "click_rate":  f"{safe_float(r['UClickRate']):.2f}%",
            "unsubs":      safe_int(r["Unsubscribed"]),
            "bounced":     safe_int(r["Bounced"]),
            "url":         str(r.get("URL") or ""),
        }
        for r in camp_rows
    ]
    camp_table.sort(key=lambda x: x["sent_date"], reverse=True)
    M["campaign_table"] = camp_table

    def _art_rows(rows):
        mx = max((safe_int(r["unique_clicks"]) for r in rows), default=1) or 1
        return [
            {
                "rank":             i + 1,
                "title":            str(r["article_title"] or "Unknown"),
                "issue":            str(r.get("issue_name") or ""),
                "url":              str(r["url"] or ""),
                "type":             str(r.get("type") or "unknown").title(),
                "story_position":   safe_int(r["story_position"]),
                "position_category":str(r["position_category"] or ""),
                "unique_clicks":    safe_int(r["unique_clicks"]),
                "total_clicks":     safe_int(r["non_unique_clicks"]),
                "bar_width":        f"{round(100.0 * safe_int(r['unique_clicks']) / mx)}%",
            }
            for i, r in enumerate(rows)
        ]

    M["top_articles"] = _art_rows(article_rows)
    M["content_drill_table"] = [
        {
            "title":            str(r["title"] or "Unknown"),
            "url":              str(r["url"] or ""),
            # Issue this row's clicks were recorded against — one row per
            # (article, issue) placement, so the same article can appear
            # multiple times in this table if it was placed in several sends.
            "issue_name":       str(r["issue_name"] or ""),
            "issue_date":       str(r["issue_date"]) if r.get("issue_date") else "",
            "unique_clicks":    safe_int(r["unique_clicks"]),
            "total_clicks":     safe_int(r["non_unique_clicks"]),
            "story_position":   safe_int(r["story_position"]),
            "position_category":str(r["position_category"] or ""),
            "categories":       str(r["categories"] or "Uncategorized"),
            "tags":             str(r["tags"] or ""),
            "written_by":       str(r["written_by"] or "Unknown"),
        }
        for r in content_drill_rows
    ]

    # Acquisition quality — per-source metrics with optional time window.
    # Returns the per-source rows plus 30d/90d churn fields. Backwards-
    # compatible aliases (`unique_clicks` etc.) are kept on each row so older
    # HTML reading the legacy field names doesn't break.
    def _row(r):
        subs = safe_int(r["subscribers"])
        c30  = safe_int(r["churned_30d"])
        c90  = safe_int(r["churned_90d"])
        clicks = safe_int(r["clicks"])
        return {
            "label":        r["label"],
            "subscribers":  subs,
            "clickers":     safe_int(r["clickers"]),
            "clicks":       clicks,
            # Legacy aliases for older HTML — both point at the windowed click
            # count now (raw events). The data model used to distinguish
            # unique vs non-unique via the pre-aggregated subscriber_clicks
            # rollup; that table no longer participates here.
            "unique_clicks":     clicks,
            "non_unique_clicks": clicks,
            "avg_clicks_per_subscriber":        safe_float(r["avg_clicks_per_subscriber"]),
            "avg_unique_clicks_per_subscriber": safe_float(r["avg_clicks_per_subscriber"]),
            "clicker_rate":   f"{safe_float(r['clicker_rate']) or 0:.1f}%",
            "churned_30d":      c30,
            "churned_30d_rate": round((c30 / subs * 100), 1) if subs else 0.0,
            "churned_90d":      c90,
            "churned_90d_rate": round((c90 / subs * 100), 1) if subs else 0.0,
        }

    def acquisition_payload(rows_all, rows_30, rows_60, rows_90):
        return {
            # Top-level arrays + `rows` are kept for backwards compatibility
            # with HTML that hasn't been updated yet — they mirror rows_all.
            "labels":           [r["label"]       for r in rows_all],
            "subscribers":      [safe_int(r["subscribers"]) for r in rows_all],
            "clickers":         [safe_int(r["clickers"])    for r in rows_all],
            "unique_clicks":    [safe_int(r["clicks"])      for r in rows_all],
            "non_unique_clicks":[safe_int(r["clicks"])      for r in rows_all],
            "avg_unique_clicks_per_subscriber": [safe_float(r["avg_clicks_per_subscriber"]) for r in rows_all],
            "clicker_rate":     [safe_float(r["clicker_rate"]) for r in rows_all],
            "rows":     [_row(r) for r in rows_all],
            "rows_all": [_row(r) for r in rows_all],
            "rows_30d": [_row(r) for r in rows_30],
            "rows_60d": [_row(r) for r in rows_60],
            "rows_90d": [_row(r) for r in rows_90],
        }

    M["acquisition_quality"] = {
        "utm_source": acquisition_payload(
            acquisition_utm_rows,
            acquisition_utm_rows_30,
            acquisition_utm_rows_60,
            acquisition_utm_rows_90,
        ),
    }

    # UTM source subscriber click performance
    M["utm_clicks_performance"] = {
        "labels":        [r["label"] for r in sub_clicks_utm_rows],
        "clickers":      [safe_int(r["clickers"]) for r in sub_clicks_utm_rows],
        "unique_clicks": [safe_int(r["unique_clicks"]) for r in sub_clicks_utm_rows],
        "total_clicks":  [safe_int(r["total_clicks"]) for r in sub_clicks_utm_rows],
        "avg_per_clicker":[safe_float(r["avg_per_clicker"]) for r in sub_clicks_utm_rows],
    }

    # Quiz distributions
    M["quiz_age_dist"] = {
        "labels": [r["bucket"] for r in quiz_age_rows],
        "data":   [safe_int(r["cnt"]) for r in quiz_age_rows],
        "colors": color_list(len(quiz_age_rows)),
    }
    gender_colors = {"Male": "#4f8cff", "Female": "#f472b6", "Unknown": "#6b7280"}
    M["quiz_gender_dist"] = {
        "labels": [r["gender"] for r in quiz_gender_rows],
        "data":   [safe_int(r["cnt"]) for r in quiz_gender_rows],
        "colors": [gender_colors.get(r["gender"], "#a78bfa") for r in quiz_gender_rows],
    }
    M["quiz_exercise_dist"] = {
        "labels": [r["freq"] for r in exercise_rows],
        "data":   [safe_int(r["cnt"]) for r in exercise_rows],
    }
    M["quiz_sleep_dist"] = {
        "labels": [r["sleep"] for r in sleep_rows],
        "data":   [safe_int(r["cnt"]) for r in sleep_rows],
    }
    M["quiz_education_dist"] = {
        "labels": [r["label"] for r in education_rows],
        "data":   [safe_int(r["cnt"]) for r in education_rows],
        "colors": color_list(len(education_rows)),
    }
    M["quiz_marital_dist"] = {
        "labels": [r["label"] for r in marital_rows],
        "data":   [safe_int(r["cnt"]) for r in marital_rows],
        "colors": color_list(len(marital_rows)),
    }
    M["quiz_obesity_dist"] = {
        "labels": [r["label"] for r in obesity_rows],
        "data":   [safe_int(r["cnt"]) for r in obesity_rows],
        "colors": color_list(len(obesity_rows)),
    }

    # Revenue
    M["revenue_monthly"] = {
        "labels":  [r["month_label"] for r in rev_monthly_rows],
        "revenue": [safe_float(r["revenue"]) for r in rev_monthly_rows],
        "deals":   [safe_int(r["deals"]) for r in rev_monthly_rows],
    }
    max_sp = max((safe_float(r["revenue"]) for r in sponsor_rows if r.get("revenue") is not None), default=1) or 1
    M["top_sponsors"] = [
        {
            "name":      str(r["sponsor"] or "Unknown")[:35],
            "deals":     safe_int(r["deals"]),
            "revenue":   f"${safe_float(r['revenue']):,.0f}",
            "bar_width": f"{round(safe_float(r['revenue']) / max_sp * 100)}%",
        }
        for r in sponsor_rows
    ]
    M["sponsor_type_dist"] = {
        "labels": [r["stype"] for r in sponsor_type_rows],
        "data":   [safe_int(r["cnt"]) for r in sponsor_type_rows],
        "colors": color_list(len(sponsor_type_rows)),
    }

    # Retention (aggregated across all subscribers, no sub_level split)
    r = retention_overall_row
    rt_total   = safe_int(r.get("total"))
    rt_active  = safe_int(r.get("active"))
    rt_churned = safe_int(r.get("churned"))
    M["retention_overall"] = {
        "total":                rt_total,
        "active":               rt_active,
        "churned":              rt_churned,
        "active_rate":          pct(rt_active,  rt_total),
        "churn_rate":           pct(rt_churned, rt_total),
        "avg_lifespan_days":    safe_int(r.get("avg_lifespan_days")),
        "median_lifespan_days": safe_int(r.get("median_lifespan_days")),
    }

    dist_keys   = ["d0_30", "d31_90", "d91_180", "d181_365", "d365plus"]
    dist_labels = ["0–30d", "31–90d", "91–180d", "181–365d", "365d+"]
    M["lifespan_dist"] = {
        "labels": dist_labels,
        "data":   [safe_int(lifespan_dist_row.get(k)) for k in dist_keys],
    }

    sv = survival_row
    sv_total = safe_int(sv.get("total")) or 1
    M["survival_curve"] = {
        "labels": ["Day 0", "Day 30", "Day 60", "Day 90", "Day 180", "Day 365"],
        "rates":  [
            100.0,
            round(safe_int(sv.get("alive_30"))  / sv_total * 100, 1),
            round(safe_int(sv.get("alive_60"))  / sv_total * 100, 1),
            round(safe_int(sv.get("alive_90"))  / sv_total * 100, 1),
            round(safe_int(sv.get("alive_180")) / sv_total * 100, 1),
            round(safe_int(sv.get("alive_365")) / sv_total * 100, 1),
        ],
    }

    # Per-source overlay: one rates[] per acquisition source bucket (top 8
    # by cohort size, minimum 500 subscribers). Same Day 0/30/60/90/180/365
    # labels as the overall curve so the dashboard can overlay them on one
    # chart with a legend.
    M["survival_curve_by_source"] = {
        "labels": ["Day 0", "Day 30", "Day 60", "Day 90", "Day 180", "Day 365"],
        "series": [
            {
                "label":  str(r["bucket"]),
                "total":  safe_int(r["total"]),
                "rates":  [
                    100.0,
                    round(safe_int(r["alive_30"])  / max(safe_int(r["total"]), 1) * 100, 1),
                    round(safe_int(r["alive_60"])  / max(safe_int(r["total"]), 1) * 100, 1),
                    round(safe_int(r["alive_90"])  / max(safe_int(r["total"]), 1) * 100, 1),
                    round(safe_int(r["alive_180"]) / max(safe_int(r["total"]), 1) * 100, 1),
                    round(safe_int(r["alive_365"]) / max(safe_int(r["total"]), 1) * 100, 1),
                ],
            }
            for r in survival_by_source_rows
        ],
    }

    M["monthly_churn"] = {
        "labels":     [str(r["month"]) for r in churn_monthly_rows],
        "data":       [safe_int(r["churned"]) for r in churn_monthly_rows],
        # Per-month send volume (sum of Recipients across campaigns with
        # Recipients > 95). 0 when no qualifying send happened in the month.
        "total_sent": [safe_int(r["total_sent"]) for r in churn_monthly_rows],
        # Number of qualifying campaigns sent in the month.
        "campaigns":  [safe_int(r["campaigns"]) for r in churn_monthly_rows],
        # List churn / sends — subscribers.date_unsubscribed (ALL channels)
        # divided by Campaigns.Recipients sum. None when no qualifying
        # campaign was sent in the month (avoids divide-by-zero confusion).
        "churn_pct":  [safe_float(r["churn_pct"]) if r.get("churn_pct") is not None else None
                       for r in churn_monthly_rows],
        # Campaign-attributed unsubs (sum of Campaigns.Unsubscribed in the month).
        "campaign_unsubs": [safe_int(r["campaign_unsubs"]) for r in churn_monthly_rows],
        # Pure-Campaigns rate — both numerator and denominator from
        # `Campaigns` (Unsubscribed / Recipients * 100). None when no
        # qualifying send happened in the month.
        "campaign_unsub_pct": [safe_float(r["campaign_unsub_pct"]) if r.get("campaign_unsub_pct") is not None else None
                               for r in churn_monthly_rows],
    }

    # ── Retention by Acquisition Source ──
    M["retention_by_source"] = [
        {
            "source":               str(r["bucket"]),
            "subscribers":          safe_int(r["subscribers"]),
            "active_now":           safe_int(r["active_now"]),
            "churned":              safe_int(r["churned"]),
            "unsub_15d":            safe_int(r["unsub_15d"]),
            "unsub_30d":            safe_int(r["unsub_30d"]),
            "unsub_15d_rate":       round(safe_int(r["unsub_15d"]) / safe_int(r["subscribers"]) * 100, 1) if safe_int(r["subscribers"]) else 0.0,
            "unsub_30d_rate":       round(safe_int(r["unsub_30d"]) / safe_int(r["subscribers"]) * 100, 1) if safe_int(r["subscribers"]) else 0.0,
            "avg_lifespan_days":    safe_float(r["avg_lifespan_days"]),
            "median_lifespan_days": safe_float(r["median_lifespan_days"]),
            "total_unique_clicks":  safe_int(r["total_unique_clicks"]),
            "clickers":             safe_int(r["clickers"]),
            "clicker_rate":         round(safe_int(r["clickers"]) / safe_int(r["subscribers"]) * 100, 1) if safe_int(r["subscribers"]) else 0.0,
            "avg_clicks_per_clicker": round(safe_int(r["total_unique_clicks"]) / safe_int(r["clickers"]), 1) if safe_int(r["clickers"]) else 0.0,
        }
        for r in retention_by_source_rows
    ]

    # ── Cohort heatmap ──
    today = date.today()
    heatmap_rows = []
    month_offsets = [1, 2, 3, 4, 5, 6, 9, 12]
    offset_days   = [30, 60, 90, 120, 150, 180, 270, 365]
    alive_keys    = ["alive_m1","alive_m2","alive_m3","alive_m4","alive_m5","alive_m6","alive_m9","alive_m12"]

    for r in cohort_heatmap_rows:
        total = safe_int(r["cohort_size"])
        if not total:
            continue
        row_out = {
            "cohort": str(r["cohort_month"])[:7],
            "size":   total,
        }
        for mo, days, key in zip(month_offsets, offset_days, alive_keys):
            cohort_age_days = (today - r["cohort_month"]).days
            if cohort_age_days < days:
                row_out[f"m{mo}"] = None
            else:
                row_out[f"m{mo}"] = round(safe_int(r[key]) / total * 100, 1)
        heatmap_rows.append(row_out)

    M["cohort_heatmap"] = heatmap_rows

    # ── Cohort table ── (2025+ only; see Q40 in METRICS_updated.md)
    M["cohort_table"] = [
        {
            "cohort":             str(r["cohort_label"]),
            "size":               safe_int(r["total"]),
            "total_subscribers":  safe_int(r["total_subscribers"]),
            "active_now":         safe_int(r["active_now"]),
            "churned":            safe_int(r["churned"]),
            "active_pct":         f"{safe_float(r['active_pct']):.1f}%",
            "churn_rate_pct":     f"{safe_float(r['churn_rate_pct']):.1f}%",
            "early_churn_pct":    f"{safe_float(r['early_churn_pct']):.1f}%",
            "campaigns_sent":     safe_int(r["campaigns_sent"]),
        }
        for r in cohort_table_rows
    ]

    # ── Cohort KPIs ──
    valid_m3 = [r for r in heatmap_rows if r.get("m3") is not None and r["size"] >= 20]
    best_cohort  = max(valid_m3, key=lambda r: r["m3"], default=None)
    worst_cohort = min(valid_m3, key=lambda r: r["m3"], default=None)
    avg_90d = round(sum(r["m3"] for r in valid_m3) / len(valid_m3), 1) if valid_m3 else None
    M["cohort_kpis"] = {
        "total_cohorts":      len(heatmap_rows),
        "avg_90d_retention":  f"{avg_90d:.1f}%" if avg_90d is not None else "—",
        "best_cohort_label":  best_cohort["cohort"] if best_cohort else "—",
        "best_cohort_m3":     f"{best_cohort['m3']:.1f}%" if best_cohort else "—",
        "worst_cohort_label": worst_cohort["cohort"] if worst_cohort else "—",
        "worst_cohort_m3":    f"{worst_cohort['m3']:.1f}%" if worst_cohort else "—",
    }

    body = json.dumps(M, indent=2, default=str)
    commit_to_github(body)

    logger.info(
        "Done — subscribers=%d campaigns=%d revenue=%.0f quiz=%d article_clicks=%d",
        total_subscribers, total_campaigns, total_revenue, quiz_count, total_article_clicks,
    )
    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "data_as_of": M["data_as_of"],
            "total_subscribers": total_subscribers,
            "total_campaigns": total_campaigns,
            "total_revenue": total_revenue,
            "quiz_count": quiz_count,
        }),
    }
