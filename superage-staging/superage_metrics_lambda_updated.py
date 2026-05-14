"""
SuperAge Dashboard — superage-metrics.json Refresh Lambda
=========================================================
Queries SuperAge dashboard tables, computes metrics, and commits
superage-staging/superage-metrics.json to GitHub.

Changes in this version:
  - Campaigns filter: requires Recipients > 1000 (removes test/small sends).
  - Acquisition quality grouped by utm_source only (o_event and sub_source removed).
  - New utm_clicks_performance section: which utm_source drives the most article
    clicks and unique clicks from subscriber_clicks joined to subscribers.
  - Column marital_status used for marital status in subscriber_quiz.
  - Revenue table uses issue_date (not invoice_month).
  - GITHUB_FILE_PATH defaults to superage-staging/superage-metrics.json.

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

        # Active (send-to): engagement_segment NOT IN ('Ghosts','Zombies','Dormant').
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (
                    WHERE engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant')
                ) AS send_to_active
            FROM {S}.subscribers
            WHERE date_joined::date < CURRENT_DATE
        """)
        send_to_active = safe_int((cur.fetchone() or {}).get("send_to_active"))
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

        cur.execute(f"""
            SELECT DATE_TRUNC('month', date_joined)::date AS month, COUNT(*) AS cnt
            FROM {S}.subscribers
            WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
            GROUP BY 1 ORDER BY 1
        """)
        growth_rows = cur.fetchall()

        cur.execute(f"""
            SELECT DATE_TRUNC('month', date_unsubscribed)::date AS month, COUNT(*) AS cnt
            FROM {S}.subscribers
            WHERE date_unsubscribed IS NOT NULL AND date_unsubscribed::date < CURRENT_DATE
            GROUP BY 1 ORDER BY 1
        """)
        unsub_rows = cur.fetchall()

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

        cur.execute(f"""
            SELECT COALESCE(NULLIF(sub_level, ''), 'Unknown') AS level, COUNT(*) AS cnt
            FROM {S}.subscribers
            WHERE date_joined::date < CURRENT_DATE
            GROUP BY 1 ORDER BY 2 DESC
        """)
        level_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(NULLIF(sub_source, ''), 'Direct/Unknown') AS source, COUNT(*) AS cnt
            FROM {S}.subscribers
            WHERE date_joined::date < CURRENT_DATE
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """)
        source_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COUNT(*) AS n
            FROM {S}.subscribers
            WHERE us_based_notification = 'Yes'
              AND date_joined::date < CURRENT_DATE
        """)
        us_based_count = safe_int((cur.fetchone() or {}).get("n"))

        # ─────────────────────────────────────────────────────
        # 2. Campaigns — filter: Recipients > 1000
        # ─────────────────────────────────────────────────────
        camp_filter = """
            "Sent Date " IS NOT NULL
            AND "Sent Date "::date < CURRENT_DATE
            AND "Recipients" > 1000
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

        cur.execute(f"""
            SELECT
                "Campaign Name", "Sent Date ", "Recipients", "UniqueOpened",
                "Clicks", "Unsubscribed", "UOpenRate", "UClickRate",
                COALESCE("URL", '') AS "URL"
            FROM {S}."Campaigns"
            WHERE {camp_filter}
            ORDER BY "UOpenRate" DESC NULLS LAST
            LIMIT 15
        """)
        top_open_rows = cur.fetchall()

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
                COALESCE(NULLIF(type, ''), 'unknown') AS content_type,
                COUNT(*) AS placements,
                COALESCE(SUM(unique_clicks), 0) AS unique_clicks,
                COALESCE(SUM(non_unique_clicks), 0) AS non_unique_clicks,
                ROUND(COALESCE(SUM(unique_clicks), 0)::numeric / NULLIF(COUNT(*), 0), 1) AS avg_unique_clicks
            FROM {S}.articles_clicks ac
            WHERE {ac_type_excl}
            GROUP BY 1
            ORDER BY unique_clicks DESC NULLS LAST
        """)
        content_type_rows = cur.fetchall()

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

        # Windowed article clicks — 7d, 15d, 30d, 90d using issue_date
        def _fetch_windowed_articles(days):
            cur.execute(f"""
                SELECT
                    article_title, url,
                    MAX(type) AS type,
                    MAX(story_position) AS story_position,
                    MAX(position_category) AS position_category,
                    SUM(unique_clicks) AS unique_clicks,
                    SUM(non_unique_clicks) AS non_unique_clicks
                FROM {S}.articles_clicks
                WHERE {ac_type_excl}
                  AND issue_date >= CURRENT_DATE - INTERVAL '{days} days'
                GROUP BY article_title, url
                ORDER BY unique_clicks DESC NULLS LAST
                LIMIT 40
            """)
            return cur.fetchall()

        windowed_articles = {
            "7d":  _fetch_windowed_articles(7),
            "15d": _fetch_windowed_articles(15),
            "30d": _fetch_windowed_articles(30),
            "90d": _fetch_windowed_articles(90),
        }

        # Article click comparison: this week vs prev week, this month vs last month
        cur.execute(f"""
            SELECT
                COALESCE(SUM(unique_clicks) FILTER (
                    WHERE issue_date >= DATE_TRUNC('week', CURRENT_DATE)
                      AND issue_date < CURRENT_DATE
                ), 0) AS this_week_clicks,
                COALESCE(SUM(unique_clicks) FILTER (
                    WHERE issue_date >= DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '7 days'
                      AND issue_date < DATE_TRUNC('week', CURRENT_DATE)
                ), 0) AS prev_week_clicks,
                COALESCE(SUM(unique_clicks) FILTER (
                    WHERE issue_date >= DATE_TRUNC('month', CURRENT_DATE)
                      AND issue_date < CURRENT_DATE
                ), 0) AS this_month_clicks,
                COALESCE(SUM(unique_clicks) FILTER (
                    WHERE issue_date >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month'
                      AND issue_date < DATE_TRUNC('month', CURRENT_DATE)
                ), 0) AS prev_month_clicks
            FROM {S}.articles_clicks
            WHERE {ac_type_excl}
        """)
        click_comparison = cur.fetchone() or {}


        # WordPress category counts
        cur.execute(f"""
            WITH wa AS (
                SELECT
                    article_url,
                    categories,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.wordpress_articles
                WHERE {wp_filter}
            ), cats AS (
                SELECT
                    NULLIF(TRIM(cat), '') AS label,
                    COUNT(DISTINCT article_url) AS article_count
                FROM wa
                CROSS JOIN LATERAL regexp_split_to_table(
                    COALESCE(NULLIF(categories, ''), 'Uncategorized'), '\\\\s*,\\\\s*'
                ) AS cat
                WHERE NULLIF(TRIM(cat), '') IS NOT NULL
                GROUP BY 1
            )
            SELECT label, article_count FROM cats
            ORDER BY article_count DESC, label
            LIMIT 15
        """)
        wp_category_count_rows = cur.fetchall()

        # WordPress category clicks (articles_clicks joined to wordpress_articles)
        cur.execute(f"""
            WITH ac AS (
                SELECT
                    article_title, url, unique_clicks, non_unique_clicks,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.articles_clicks ac
                WHERE {ac_type_excl}
            ), wa AS (
                SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''))
                    article_url, categories,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.wordpress_articles
                WHERE {wp_filter}
                ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''), modified_date DESC NULLS LAST
            ), joined AS (
                SELECT
                    COALESCE(NULLIF(ac.norm_url, ''), ac.article_title) AS article_key,
                    ac.unique_clicks, ac.non_unique_clicks, wa.categories
                FROM ac LEFT JOIN wa ON ac.norm_url = wa.norm_url
            ), cats AS (
                SELECT
                    NULLIF(TRIM(cat), '') AS label,
                    COUNT(DISTINCT article_key) AS article_count,
                    COALESCE(SUM(unique_clicks), 0) AS unique_clicks,
                    COALESCE(SUM(non_unique_clicks), 0) AS non_unique_clicks,
                    ROUND(COALESCE(SUM(unique_clicks), 0)::numeric / NULLIF(COUNT(DISTINCT article_key), 0), 1) AS avg_unique_clicks
                FROM joined
                CROSS JOIN LATERAL regexp_split_to_table(
                    COALESCE(NULLIF(categories, ''), 'Uncategorized'), '\\\\s*,\\\\s*'
                ) AS cat
                WHERE NULLIF(TRIM(cat), '') IS NOT NULL
                GROUP BY 1
            )
            SELECT label, article_count, unique_clicks, non_unique_clicks, avg_unique_clicks
            FROM cats
            ORDER BY unique_clicks DESC, label
            LIMIT 15
        """)
        wp_category_click_rows = cur.fetchall()

        # WordPress tag clicks
        cur.execute(f"""
            WITH ac AS (
                SELECT
                    article_title, url, unique_clicks, non_unique_clicks,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.articles_clicks ac
                WHERE {ac_type_excl}
            ), wa AS (
                SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''))
                    article_url, tags,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.wordpress_articles
                WHERE {wp_filter}
                ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''), modified_date DESC NULLS LAST
            ), joined AS (
                SELECT
                    COALESCE(NULLIF(ac.norm_url, ''), ac.article_title) AS article_key,
                    ac.unique_clicks, ac.non_unique_clicks, wa.tags
                FROM ac LEFT JOIN wa ON ac.norm_url = wa.norm_url
            ), tag_rows AS (
                SELECT
                    NULLIF(TRIM(tag), '') AS label,
                    COUNT(DISTINCT article_key) AS article_count,
                    COALESCE(SUM(unique_clicks), 0) AS unique_clicks,
                    COALESCE(SUM(non_unique_clicks), 0) AS non_unique_clicks,
                    ROUND(COALESCE(SUM(unique_clicks), 0)::numeric / NULLIF(COUNT(DISTINCT article_key), 0), 1) AS avg_unique_clicks
                FROM joined
                CROSS JOIN LATERAL regexp_split_to_table(
                    COALESCE(NULLIF(tags, ''), 'Untagged'), '\\\\s*,\\\\s*'
                ) AS tag
                WHERE NULLIF(TRIM(tag), '') IS NOT NULL
                GROUP BY 1
            )
            SELECT label, article_count, unique_clicks, non_unique_clicks, avg_unique_clicks
            FROM tag_rows
            ORDER BY unique_clicks DESC, label
            LIMIT 20
        """)
        wp_tag_click_rows = cur.fetchall()

        # Writer clicks (grouped by written_by)
        cur.execute(f"""
            WITH ac AS (
                SELECT
                    url, unique_clicks, non_unique_clicks,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.articles_clicks
                WHERE {ac_type_excl}
            ), wa AS (
                SELECT DISTINCT ON (REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''))
                    COALESCE(NULLIF(TRIM(written_by), ''), 'Unknown') AS written_by,
                    REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', '') AS norm_url
                FROM {S}.wordpress_articles
                WHERE {wp_filter}
                ORDER BY REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART(article_url, '?', 1))), '^https?://(www[.])?', ''), modified_date DESC NULLS LAST
            )
            SELECT
                COALESCE(wa.written_by, 'Unknown') AS label,
                COUNT(DISTINCT ac.norm_url) AS article_count,
                COALESCE(SUM(ac.unique_clicks), 0) AS unique_clicks,
                COALESCE(SUM(ac.non_unique_clicks), 0) AS non_unique_clicks,
                ROUND(COALESCE(SUM(ac.unique_clicks), 0)::numeric / NULLIF(COUNT(DISTINCT ac.norm_url), 0), 1) AS avg_unique_clicks
            FROM ac
            LEFT JOIN wa ON ac.norm_url = wa.norm_url
            GROUP BY 1
            ORDER BY unique_clicks DESC
            LIMIT 20
        """)
        wp_writer_click_rows = cur.fetchall()

        # Content drill table (article-level, all metadata for client-side filtering)
        # INNER JOIN to wordpress_articles — articles_clicks also tracks sponsor
        # placements that aren't WordPress articles; those are out of scope here.
        cur.execute(f"""
            WITH ac AS (
                SELECT
                    article_title, url, unique_clicks, non_unique_clicks,
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

        low_position_rows = []  # Hidden Gems section removed

        # Subscriber click distribution
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE unique_clicks = 1)              AS c1,
                COUNT(*) FILTER (WHERE unique_clicks BETWEEN 2 AND 5)  AS c2_5,
                COUNT(*) FILTER (WHERE unique_clicks BETWEEN 6 AND 10) AS c6_10,
                COUNT(*) FILTER (WHERE unique_clicks BETWEEN 11 AND 20) AS c11_20,
                COUNT(*) FILTER (WHERE unique_clicks > 20)             AS c20plus,
                COUNT(*) AS total_clickers
            FROM {S}.subscriber_clicks
        """)
        click_dist = cur.fetchone() or {}
        total_article_clickers = safe_int(click_dist.get("total_clickers"))

        # ─────────────────────────────────────────────────────
        # 4. Acquisition quality — utm_source only
        # ─────────────────────────────────────────────────────
        def fetch_acquisition_rows(label_expr: str, fallback_label: str, limit: int = 12):
            # label_expr is the SQL expression(s) to coalesce *before* the
            # fallback label, e.g. "NULLIF(TRIM(utm_source),''), NULLIF(TRIM(source),''))".
            cur.execute(f"""
                WITH s AS (
                    SELECT
                        LOWER(TRIM(email)) AS email,
                        COALESCE({label_expr}, %s) AS label
                    FROM {S}.subscribers
                    WHERE email IS NOT NULL AND TRIM(email) != ''
                      AND date_joined::date < CURRENT_DATE
                ), sc AS (
                    SELECT
                        LOWER(TRIM(email_address)) AS email,
                        SUM(unique_clicks)     AS unique_clicks,
                        SUM(non_unique_clicks) AS non_unique_clicks
                    FROM {S}.subscriber_clicks
                    WHERE email_address IS NOT NULL AND TRIM(email_address) != ''
                    GROUP BY 1
                )
                SELECT
                    s.label,
                    COUNT(*) AS subscribers,
                    COUNT(sc.email) AS clickers,
                    COALESCE(SUM(sc.unique_clicks), 0)     AS unique_clicks,
                    COALESCE(SUM(sc.non_unique_clicks), 0) AS non_unique_clicks,
                    ROUND(COALESCE(SUM(sc.unique_clicks), 0)::numeric / NULLIF(COUNT(*), 0), 2) AS avg_unique_clicks_per_subscriber,
                    ROUND(COUNT(sc.email)::numeric / NULLIF(COUNT(*), 0) * 100, 1) AS clicker_rate
                FROM s LEFT JOIN sc ON s.email = sc.email
                GROUP BY 1
                ORDER BY unique_clicks DESC NULLS LAST, subscribers DESC
                LIMIT {int(limit)}
            """, (fallback_label,))
            return cur.fetchall()

        # Acquisition source label: utm_source → source → 'Organic'
        acquisition_utm_rows = fetch_acquisition_rows(
            "NULLIF(TRIM(utm_source),''), NULLIF(TRIM(source),'')",
            "Organic",
        )

        # ─────────────────────────────────────────────────────
        # 5. UTM source subscriber click performance
        #    Which UTM source drives the most article clicks
        #    from subscriber_clicks joined to subscribers.
        # ─────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(s.utm_source), ''), NULLIF(TRIM(s.source), ''), 'Organic') AS label,
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
        cur.execute(f"""
            SELECT
                COUNT(*) AS n,
                ROUND(AVG(longevity_score)::numeric, 1) AS avg_score,
                ROUND(MIN(longevity_score)::numeric, 1) AS min_score,
                ROUND(MAX(longevity_score)::numeric, 1) AS max_score,
                ROUND(AVG(age)::numeric, 1) AS avg_age
            FROM {S}.subscriber_quiz
            WHERE longevity_score IS NOT NULL
        """)
        qs = cur.fetchone() or {}
        quiz_count   = safe_int(qs.get("n"))
        avg_score    = safe_float(qs.get("avg_score"))
        avg_age_quiz = safe_float(qs.get("avg_age"))
        max_score    = safe_float(qs.get("max_score"))

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
                COUNT(*) FILTER (WHERE longevity_score < 60) AS s_under60,
                COUNT(*) FILTER (WHERE longevity_score BETWEEN 60 AND 69.99) AS s_60_70,
                COUNT(*) FILTER (WHERE longevity_score BETWEEN 70 AND 79.99) AS s_70_80,
                COUNT(*) FILTER (WHERE longevity_score BETWEEN 80 AND 89.99) AS s_80_90,
                COUNT(*) FILTER (WHERE longevity_score >= 90) AS s_90plus
            FROM {S}.subscriber_quiz
            WHERE longevity_score IS NOT NULL
        """)
        score_dist = cur.fetchone() or {}

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
        cur.execute(f"""
            SELECT
                COUNT(*) AS n,
                SUM(NULLIF("$_line_amount", '')::numeric) AS total_revenue,
                AVG(NULLIF("$_line_amount", '')::numeric) AS avg_deal,
                MAX(NULLIF("$_line_amount", '')::numeric) AS max_deal
            FROM {S}.sa_airtable_sales
            WHERE "$_line_amount" IS NOT NULL AND "$_line_amount" != ''
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
              AND "$_line_amount" IS NOT NULL AND "$_line_amount" != ''
            GROUP BY 1, 2
            ORDER BY 1
        """)
        rev_monthly_rows = cur.fetchall()

        cur.execute(f"""
            SELECT
                COALESCE("sponsor_name"->>0, "sponsor_name"::text, 'Unknown') AS sponsor,
                COUNT(*) AS deals,
                SUM(NULLIF("$_line_amount", '')::numeric) AS revenue,
                AVG(NULLIF(ecpm, '')::numeric) AS avg_ecpm
            FROM {S}.sa_airtable_sales
            WHERE "$_line_amount" IS NOT NULL AND "$_line_amount" != ''
            GROUP BY 1 ORDER BY 3 DESC NULLS LAST LIMIT 10
        """)
        sponsor_rows = cur.fetchall()

        cur.execute(f"""
            SELECT COALESCE(NULLIF(sponsor_type, ''), 'Unknown') AS stype, COUNT(*) AS cnt
            FROM {S}.sa_airtable_sales
            GROUP BY 1 ORDER BY 2 DESC
        """)
        sponsor_type_rows = cur.fetchall()

        cur.execute(f"""
            SELECT
                ROUND(AVG(NULLIF(ecpm, '')::numeric)::numeric, 2) AS avg_ecpm
            FROM {S}.sa_airtable_sales
        """)
        perf = cur.fetchone() or {}
        avg_ecpm = safe_float(perf.get("avg_ecpm"))

        # ─────────────────────────────────────────────────────
        # 8. Retention
        # ─────────────────────────────────────────────────────
        # Retention is aggregated across ALL subscribers (no sub_level split).
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
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

        cur.execute(f"""
            SELECT
                DATE_TRUNC('month', date_unsubscribed)::date AS month,
                COUNT(*) AS churned
            FROM {S}.subscribers
            WHERE state = 'Unsubscribed'
              AND date_unsubscribed IS NOT NULL AND date_unsubscribed::date < CURRENT_DATE
            GROUP BY 1 ORDER BY 1
        """)
        churn_monthly_rows = cur.fetchall()

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

        # Cohort churn rate table (best / worst retention cohorts) + campaigns sent that month
        cur.execute(f"""
            WITH cohorts AS (
                SELECT
                    TO_CHAR(DATE_TRUNC('month', date_joined::date), 'Mon YYYY') AS cohort_label,
                    DATE_TRUNC('month', date_joined::date)::date AS cohort_month,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE state = 'Active') AS active_now,
                    COUNT(*) FILTER (WHERE state = 'Unsubscribed') AS churned,
                    COUNT(*) FILTER (WHERE unsubbed_within_90) AS churned_90d
                FROM (
                    SELECT
                        date_joined,
                        state,
                        (
                            state = 'Unsubscribed'
                            AND date_unsubscribed IS NOT NULL
                            AND date_unsubscribed::date <= date_joined::date + 90
                        ) AS unsubbed_within_90
                    FROM {S}.subscribers
                    WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
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
                  AND "Recipients" > 1000
                GROUP BY 1
            )
            SELECT
                c.*,
                ROUND(c.churned::numeric / NULLIF(c.total,0) * 100, 1) AS churn_rate_pct,
                ROUND(c.active_now::numeric / NULLIF(c.total,0) * 100, 1) AS retention_pct,
                ROUND(c.churned_90d::numeric / NULLIF(c.total,0) * 100, 1) AS early_churn_pct,
                COALESCE(k.campaigns_sent, 0) AS campaigns_sent
            FROM cohorts c
            LEFT JOIN camps k ON k.camp_month = c.cohort_month
            ORDER BY c.cohort_month
        """)
        cohort_table_rows = cur.fetchall()

        # UTM source retention: avg 90-day retention rate per acquisition source
        cur.execute(f"""
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
            FROM {S}.subscribers
            WHERE date_joined IS NOT NULL AND date_joined::date < CURRENT_DATE
            GROUP BY 1
            HAVING COUNT(*) >= 100
            ORDER BY 4 DESC
            LIMIT 12
        """)
        cohort_utm_rows = cur.fetchall()

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
    M["total_all_states"]    = total_all_states           # every row in subscribers
    M["active_subscribers"]  = active_subscribers         # legacy alias
    M["send_to_active"]      = send_to_active             # engagement_segment NOT IN (Ghost/Zombie/Dormant)
    M["unsubscribed_count"]  = unsubscribed_count
    M["bounced_count"]       = bounced_count
    M["deleted_count"]       = deleted_count
    M["quiz_takers"]         = quiz_takers
    M["quiz_takers_pct"]     = round(100.0 * quiz_takers / total_subscribers, 1) if total_subscribers else 0
    M["high_engagement_60d"] = high_engagement_60d
    M["us_based_count"]      = us_based_count
    M["active_rate"]         = pct(active_subscribers, total_all_states)  # active / all states
    M["send_to_rate"]        = pct(send_to_active,    total_subscribers)  # send-to / active base

    # Campaign KPIs (Recipients > 1000)
    M["total_campaigns"]    = total_campaigns
    M["total_recipients"]   = total_recipients
    M["total_unique_opens"] = total_unique_opens
    M["total_camp_clicks"]  = total_camp_clicks
    M["total_unsubs_camp"]  = total_unsubs_camp
    M["total_bounced_camp"] = total_bounced_camp
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
    M["avg_ecpm"]           = f"${avg_ecpm:.2f}"

    # Quiz KPIs
    M["quiz_count"]          = quiz_count
    M["avg_longevity_score"] = avg_score
    M["avg_age_quiz"]        = avg_age_quiz
    M["quiz_kpis"] = {
        "total_takers":        quiz_count,
        "avg_score":           avg_score,
        "avg_age":             avg_age_quiz,
        "max_score":           max_score,
        "fitness_quiz_takers": fitness_quiz_takers,
        "menu_quiz_takers":    menu_quiz_takers,
    }

    # Subscriber state distribution
    state_colors = {
        "Active": "#34d399", "Unsubscribed": "#f87171",
        "Bounced": "#fbbf24", "Deleted": "#9ca3af", "Unknown": "#6b7280",
    }
    M["subscriber_states"] = {
        "labels": [r["state"] for r in state_rows],
        "data":   [safe_int(r["cnt"]) for r in state_rows],
        "colors": [state_colors.get(r["state"], "#a78bfa") for r in state_rows],
    }

    # Subscriber growth
    cumulative, running = [], 0
    for r in growth_rows:
        running += safe_int(r["cnt"])
        cumulative.append(running)
    M["subscriber_growth"] = {
        "labels":     [r["month"].strftime("%b %Y") for r in growth_rows],
        "data":       [safe_int(r["cnt"]) for r in growth_rows],
        "cumulative": cumulative,
    }

    # Overview subscriber growth chart — sourced from growth_history table
    M["subscriber_monthly"] = {
        "labels":       [str(r["month_label"]) for r in growth_history_rows],
        "new_subs":     [safe_int(r["gained"]) for r in growth_history_rows],
        "unsubs":       [safe_int(r["lost"]) for r in growth_history_rows],
        "active_count": [safe_int(r["total_active"]) for r in growth_history_rows],
    }

    M["sub_level_dist"] = {
        "labels": [r["level"] for r in level_rows],
        "data":   [safe_int(r["cnt"]) for r in level_rows],
        "colors": color_list(len(level_rows)),
    }
    M["source_dist"] = {
        "labels": [r["source"] for r in source_rows],
        "data":   [safe_int(r["cnt"]) for r in source_rows],
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

    max_open = max((safe_float(r["UOpenRate"]) for r in top_open_rows), default=1) or 1
    M["top_campaigns"] = [
        {
            "name":      str(r["Campaign Name"] or "")[:50],
            "sent_date": str(r["Sent Date "])[:10] if r["Sent Date "] else "",
            "recipients":safe_int(r["Recipients"]),
            "open_rate": f"{safe_float(r['UOpenRate']):.2f}%",
            "click_rate":f"{safe_float(r['UClickRate']):.2f}%",
            "bar_width": f"{round(safe_float(r['UOpenRate']) / max_open * 100)}%",
            "url":       str(r.get("URL") or ""),
        }
        for r in top_open_rows
    ]

    # Content
    content_type_labels = [str(r["content_type"] or "unknown").title() for r in content_type_rows]
    M["content_type"] = {
        "labels":           content_type_labels,
        "placements":       [safe_int(r["placements"]) for r in content_type_rows],
        "article_counts":   [safe_int(r.get("unique_articles", 0)) for r in content_type_rows],
        "unique_clicks":    [safe_int(r["unique_clicks"]) for r in content_type_rows],
        "non_unique_clicks":[safe_int(r["non_unique_clicks"]) for r in content_type_rows],
        "avg_unique_clicks":[safe_float(r["avg_unique_clicks"]) for r in content_type_rows],
        "colors":           color_list(len(content_type_rows)),
    }
    M["content_type_table"] = [
        {
            "type":              str(r["content_type"] or "unknown").title(),
            "placements":        safe_int(r["placements"]),
            "unique_articles":   safe_int(r.get("unique_articles", 0)),
            "unique_clicks":     safe_int(r["unique_clicks"]),
            "non_unique_clicks": safe_int(r["non_unique_clicks"]),
            "avg_unique_clicks": safe_float(r["avg_unique_clicks"]),
        }
        for r in content_type_rows
    ]

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

    max_art = max((safe_int(r["unique_clicks"]) for r in article_rows), default=1) or 1
    M["top_articles"] = _art_rows(article_rows)
    M["top_articles_windowed"] = {
        window: _art_rows(rows)
        for window, rows in windowed_articles.items()
    }
    M["article_click_comparison"] = {
        "this_week":    safe_int(click_comparison.get("this_week_clicks")),
        "prev_week":    safe_int(click_comparison.get("prev_week_clicks")),
        "this_month":   safe_int(click_comparison.get("this_month_clicks")),
        "prev_month":   safe_int(click_comparison.get("prev_month_clicks")),
    }
    M["article_category_counts"] = {
        "labels": [r["label"] for r in wp_category_count_rows],
        "data":   [safe_int(r["article_count"]) for r in wp_category_count_rows],
    }
    M["article_category_clicks"] = {
        "labels":           [r["label"] for r in wp_category_click_rows],
        "unique_clicks":    [safe_int(r["unique_clicks"]) for r in wp_category_click_rows],
        "non_unique_clicks":[safe_int(r["non_unique_clicks"]) for r in wp_category_click_rows],
        "article_counts":   [safe_int(r["article_count"]) for r in wp_category_click_rows],
        "avg_unique_clicks":[safe_float(r["avg_unique_clicks"]) for r in wp_category_click_rows],
    }
    M["article_tag_clicks"] = {
        "labels":           [r["label"] for r in wp_tag_click_rows],
        "unique_clicks":    [safe_int(r["unique_clicks"]) for r in wp_tag_click_rows],
        "non_unique_clicks":[safe_int(r["non_unique_clicks"]) for r in wp_tag_click_rows],
        "article_counts":   [safe_int(r["article_count"]) for r in wp_tag_click_rows],
        "avg_unique_clicks":[safe_float(r["avg_unique_clicks"]) for r in wp_tag_click_rows],
    }
    M["article_writer_clicks"] = {
        "labels":           [r["label"] for r in wp_writer_click_rows],
        "unique_clicks":    [safe_int(r["unique_clicks"]) for r in wp_writer_click_rows],
        "non_unique_clicks":[safe_int(r["non_unique_clicks"]) for r in wp_writer_click_rows],
        "article_counts":   [safe_int(r["article_count"]) for r in wp_writer_click_rows],
        "avg_unique_clicks":[safe_float(r["avg_unique_clicks"]) for r in wp_writer_click_rows],
    }
    M["content_drill_table"] = [
        {
            "title":            str(r["title"] or "Unknown"),
            "url":              str(r["url"] or ""),
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
    M["low_position_winners"] = [
        {
            "title":             str(r["article_title"] or "Unknown"),
            "issue":             str(r["issue_name"] or ""),
            "type":              str(r["type"] or "unknown").title(),
            "story_position":    safe_int(r["story_position"]),
            "position_category": str(r["position_category"] or ""),
            "unique_clicks":     safe_int(r["unique_clicks"]),
            "total_clicks":      safe_int(r["non_unique_clicks"]),
            "categories":        str(r["categories"] or ""),
            "tags":              str(r["tags"] or ""),
        }
        for r in low_position_rows
    ]

    # Click distribution
    M["click_distribution"] = {
        "labels": ["1 click", "2–5 clicks", "6–10 clicks", "11–20 clicks", "20+ clicks"],
        "data": [
            safe_int(click_dist.get("c1")),
            safe_int(click_dist.get("c2_5")),
            safe_int(click_dist.get("c6_10")),
            safe_int(click_dist.get("c11_20")),
            safe_int(click_dist.get("c20plus")),
        ],
        "colors": ["#4f8cff", "#34d399", "#a78bfa", "#fbbf24", "#f87171"],
    }

    # Acquisition quality — utm_source only
    def acquisition_payload(rows):
        return {
            "labels":     [r["label"] for r in rows],
            "subscribers":[safe_int(r["subscribers"]) for r in rows],
            "clickers":   [safe_int(r["clickers"]) for r in rows],
            "unique_clicks":    [safe_int(r["unique_clicks"]) for r in rows],
            "non_unique_clicks":[safe_int(r["non_unique_clicks"]) for r in rows],
            "avg_unique_clicks_per_subscriber": [safe_float(r["avg_unique_clicks_per_subscriber"]) for r in rows],
            "clicker_rate": [safe_float(r["clicker_rate"]) for r in rows],
            "rows": [
                {
                    "label":        r["label"],
                    "subscribers":  safe_int(r["subscribers"]),
                    "clickers":     safe_int(r["clickers"]),
                    "unique_clicks":     safe_int(r["unique_clicks"]),
                    "non_unique_clicks": safe_int(r["non_unique_clicks"]),
                    "avg_unique_clicks_per_subscriber": safe_float(r["avg_unique_clicks_per_subscriber"]),
                    "clicker_rate": f"{safe_float(r['clicker_rate']):.1f}%",
                }
                for r in rows
            ],
        }

    M["acquisition_quality"] = {
        "utm_source": acquisition_payload(acquisition_utm_rows),
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
    M["quiz_score_dist"] = {
        "labels": ["< 60", "60–70", "70–80", "80–90", "90+"],
        "data": [
            safe_int(score_dist.get("s_under60")),
            safe_int(score_dist.get("s_60_70")),
            safe_int(score_dist.get("s_70_80")),
            safe_int(score_dist.get("s_80_90")),
            safe_int(score_dist.get("s_90plus")),
        ],
        "colors": ["#f87171", "#fbbf24", "#34d399", "#4f8cff", "#a78bfa"],
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
            "avg_ecpm":  f"${safe_float(r['avg_ecpm']):.2f}" if r.get("avg_ecpm") is not None else "—",
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

    M["monthly_churn"] = {
        "labels": [str(r["month"]) for r in churn_monthly_rows],
        "data":   [safe_int(r["churned"]) for r in churn_monthly_rows],
    }

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

    # ── Cohort table ──
    M["cohort_table"] = [
        {
            "cohort":           str(r["cohort_label"]),
            "size":             safe_int(r["total"]),
            "active_now":       safe_int(r["active_now"]),
            "churned":          safe_int(r["churned"]),
            "retention_pct":    f"{safe_float(r['retention_pct']):.1f}%",
            "churn_rate_pct":   f"{safe_float(r['churn_rate_pct']):.1f}%",
            "early_churn_pct":  f"{safe_float(r['early_churn_pct']):.1f}%",
            "campaigns_sent":   safe_int(r["campaigns_sent"]),
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

    # ── UTM source retention ──
    M["cohort_utm_retention"] = [
        {
            "source":          str(r["source"]),
            "total":           safe_int(r["total"]),
            "active_now":      safe_int(r["active_now"]),
            "retention_90d":   f"{safe_float(r['retention_90d_pct']):.1f}%",
            "retention_90d_v": safe_float(r["retention_90d_pct"]),
        }
        for r in cohort_utm_rows
    ]

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
