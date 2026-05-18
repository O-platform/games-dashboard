"""
SuperAge Dashboard — superage-comparison.json Refresh Lambda
=============================================================
Generates week-over-week and day-over-day campaign performance comparisons.

Output file: superage-staging/superage-comparison.json

Rules:
  - Only campaigns with Recipients > 1,000 are included.
  - Only campaigns where Sent Date <= today - 2 days are "launched"
    (2-day minimum ensures opens/clicks have had time to accumulate).
  - Week = Monday–Sunday (ISO week).
  - "Current week" = the most recently completed ISO week that has
    at least one qualifying launched campaign.
  - "Previous week" = the week immediately before current week.
  - Day comparison: for each weekday (Mon–Sun), compare this week vs prev week.

Required env vars:
  DB_SECRET_ARN   — Secrets Manager ARN (JSON: host/port/dbname/username/password)
  GITHUB_TOKEN    — Fine-grained PAT (Contents read+write on the repo)
  GITHUB_REPO     — "O-platform/retention-dshb"
  GITHUB_BRANCH   — target branch (e.g. "main")

Optional env vars:
  DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_SSLMODE
  GITHUB_FILE_PATH  (default: superage-staging/superage-comparison.json)
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
from datetime import date, datetime, timedelta

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_db_secret_cache = None

GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "O-platform/retention-dshb")
GITHUB_FILE_PATH = os.environ.get("GITHUB_FILE_PATH", "superage-staging/superage-comparison.json")
GITHUB_BRANCH    = os.environ.get("GITHUB_BRANCH", "main")
SA_SCHEMA        = os.environ.get("SA_SCHEMA", "superage")
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
        "User-Agent": "superage-comparison-lambda",
    }

    sha = None
    try:
        req = urllib.request.Request(f"{api_base}?ref={GITHUB_BRANCH}", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            sha = json.loads(resp.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            logger.warning(f"GitHub GET failed: {e.code} {e.reason}")

    payload = {"message": f"Update superage-comparison.json — {_date_label()}", "content": base64.b64encode(content.encode()).decode(), "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(api_base, data=json.dumps(payload).encode(), headers={**headers, "Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            logger.info(f"GitHub commit OK: {resp.status}")
    except urllib.error.HTTPError as e:
        logger.error(f"GitHub PUT failed: {e.code} {e.read().decode()[:300]}")
        raise


# ─────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────

def _get_db_secret():
    global _db_secret_cache
    if _db_secret_cache:
        return _db_secret_cache
    arn = os.environ.get("DB_SECRET_ARN", "")
    if arn:
        sm = boto3.client("secretsmanager")
        _db_secret_cache = json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])
    else:
        _db_secret_cache = {}
    return _db_secret_cache


def get_conn():
    s = _get_db_secret()
    return psycopg2.connect(
        host=s.get("host", os.environ.get("DB_HOST", "localhost")),
        port=int(s.get("port", os.environ.get("DB_PORT", 5432))),
        dbname=s.get("dbname", os.environ.get("DB_NAME", "postgres")),
        user=s.get("username", os.environ.get("DB_USER", "postgres")),
        password=s.get("password", os.environ.get("DB_PASSWORD", "")),
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def safe_int(v, default=0):
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def pct_fmt(v):
    return f"{v:.2f}%" if v is not None else "—"


def delta_fmt(cur, prev):
    """Return absolute delta and direction string."""
    if prev is None or cur is None:
        return None, "—"
    d = cur - prev
    sign = "+" if d >= 0 else ""
    return round(d, 2), f"{sign}{d:.2f}"


def pct_delta(cur, prev):
    """Percentage-point delta between two rates."""
    if prev is None or cur is None:
        return None, "—"
    d = cur - prev
    sign = "+" if d >= 0 else ""
    return round(d, 2), f"{sign}{d:.2f}pp"


def week_bounds(ref_date):
    """Return (monday, sunday) for the ISO week containing ref_date."""
    monday = ref_date - timedelta(days=ref_date.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def aggregate_campaigns(rows):
    """Aggregate a list of campaign rows into summary metrics."""
    if not rows:
        return None
    n = len(rows)
    total_recipients  = sum(safe_int(r["Recipients"]) for r in rows)
    total_opens       = sum(safe_int(r["UniqueOpened"]) for r in rows)
    total_clicks      = sum(safe_int(r["Clicks"]) for r in rows)
    total_unsubs      = sum(safe_int(r["Unsubscribed"]) for r in rows)
    total_bounced     = sum(safe_int(r["Bounced"]) for r in rows)
    avg_open_rate     = safe_float(sum(safe_float(r["UOpenRate"]) for r in rows) / n) if n else 0
    avg_click_rate    = safe_float(sum(safe_float(r["UClickRate"]) for r in rows) / n) if n else 0
    best_open         = max((safe_float(r["UOpenRate"]) for r in rows), default=0)
    best_click        = max((safe_float(r["UClickRate"]) for r in rows), default=0)
    return {
        "campaigns":       n,
        "recipients":      total_recipients,
        "unique_opens":    total_opens,
        "clicks":          total_clicks,
        "unsubs":          total_unsubs,
        "bounced":         total_bounced,
        "avg_open_rate":   round(avg_open_rate, 2),
        "avg_click_rate":  round(avg_click_rate, 2),
        "best_open_rate":  round(best_open, 2),
        "best_click_rate": round(best_click, 2),
    }


def build_comparison(cur_agg, prev_agg):
    """Build a comparison dict: current, previous, and deltas."""
    if not cur_agg or not prev_agg:
        return {"current": cur_agg, "previous": prev_agg, "delta": {}}

    delta = {}
    for key in ("campaigns", "recipients", "unique_opens", "clicks", "unsubs"):
        c, p = cur_agg.get(key), prev_agg.get(key)
        d = (c - p) if (c is not None and p is not None) else None
        delta[key] = {"value": d, "pct_change": round((d / p * 100), 1) if (d is not None and p) else None}

    for key in ("avg_open_rate", "avg_click_rate"):
        c, p = cur_agg.get(key), prev_agg.get(key)
        d = round(c - p, 2) if (c is not None and p is not None) else None
        delta[key] = {"value": d, "unit": "pp"}

    return {"current": cur_agg, "previous": prev_agg, "delta": delta}


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ─────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    today = date.today()
    # A campaign must have been sent at least 2 days ago to be "launched"
    launch_cutoff = today - timedelta(days=2)

    S = SA_SCHEMA

    with get_conn() as conn:
        with conn.cursor() as cur:

            # ── Fetch all qualifying launched campaigns ──────────────────
            cur.execute(f"""
                SELECT
                    "Campaign Name",
                    "Sent Date "::date                       AS sent_date,
                    EXTRACT(DOW FROM "Sent Date "::date)     AS dow,   -- 0=Sun..6=Sat
                    EXTRACT(ISODOW FROM "Sent Date "::date)  AS isodow, -- 1=Mon..7=Sun
                    "Recipients",
                    "UniqueOpened",
                    "Clicks",
                    "Unsubscribed",
                    "Bounced",
                    "UOpenRate",
                    "UClickRate",
                    COALESCE("URL", '')                      AS "URL",
                    "Subject"
                FROM {S}."Campaigns"
                WHERE "Sent Date " IS NOT NULL
                  AND "Sent Date "::date <= %s
                  AND "Recipients" > 95
                ORDER BY "Sent Date "::date ASC
            """, (launch_cutoff,))
            all_rows = cur.fetchall()

            # ─────────────────────────────────────────────────────
            # Click Analysis trend queries (moved here from the metrics
            # lambda). Two sections:
            #  (A) Campaign-level aggregates from superage."Campaigns"
            #      → weekly (8 wks), monthly (6 mo), same-weekday (5),
            #        same day-of-month (4).
            #  (B) Raw click events from superage."Campaigns_Clicks"
            #      → same-weekday (5), weekly (4), monthly (3).
            # ─────────────────────────────────────────────────────

            # (A.1) Campaign clicks — last 8 ISO weeks
            cur.execute(f"""
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
                    FROM {S}."Campaigns"
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
                ORDER BY w.week_start
            """)
            campaign_weekly_rows = cur.fetchall()

            # (A.2) Campaign clicks — last 6 months
            cur.execute(f"""
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
                    FROM {S}."Campaigns"
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
                ORDER BY m.month_start
            """)
            campaign_monthly_rows = cur.fetchall()

            # (A.3) Campaign clicks — same weekday across last 5 occurrences
            cur.execute(f"""
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
                LEFT JOIN {S}."Campaigns" c
                  ON c."Sent Date "::date = d.day
                 AND c."Recipients" > 95
                GROUP BY d.day
                ORDER BY d.day
            """)
            campaign_same_weekday_rows = cur.fetchall()

            # (A.4) Campaign clicks — same day-of-month across last 4 months
            cur.execute(f"""
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
                LEFT JOIN {S}."Campaigns" c
                  ON c."Sent Date "::date = d.day
                 AND c."Recipients" > 95
                GROUP BY d.day
                ORDER BY d.day
            """)
            campaign_same_dom_rows = cur.fetchall()

            # (B.1) Raw clicks — same weekday across last 5 occurrences (today's weekday).
            # Kept for backwards compatibility; the dashboard now prefers raw_clicks_by_weekday below.
            # clicks_no_ss = clicks excluding Sunday Spotlight (issue_name match).
            cur.execute(f"""
                WITH clicks AS (
                    SELECT "Date"::date AS d, issue_name
                    FROM {S}."Campaigns_Clicks"
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
                    COUNT(c.d) FILTER (
                        WHERE c.issue_name NOT ILIKE '%sunday spotlight%'
                    )                             AS clicks_no_ss,
                    (d.day = CURRENT_DATE)        AS is_current
                FROM d
                LEFT JOIN clicks c ON c.d = d.day
                GROUP BY d.day
                ORDER BY d.day
            """)
            raw_clicks_same_weekday_rows = cur.fetchall()

            # (B.1b) Raw clicks — last 5 occurrences of EACH weekday (Mon–Sun).
            # The dashboard's "Same Weekday" chart lets the user pick which
            # weekday to view, so we materialise 7×5 = 35 day buckets at once.
            cur.execute(f"""
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
                    SELECT "Date"::date AS d, issue_name
                    FROM {S}."Campaigns_Clicks"
                    WHERE "Date" IS NOT NULL
                      AND "Date" >= (CURRENT_DATE - INTERVAL '6 weeks')
                      AND "Date" <= CURRENT_DATE
                )
                SELECT
                    r.day,
                    r.dow,
                    TO_CHAR(r.day, 'Dy Mon DD')        AS label,
                    COUNT(c.d)                         AS clicks,
                    COUNT(c.d) FILTER (
                        WHERE c.issue_name NOT ILIKE '%sunday spotlight%'
                    )                                  AS clicks_no_ss,
                    (r.day = CURRENT_DATE)             AS is_current
                FROM ranked r
                LEFT JOIN clicks c ON c.d = r.day
                WHERE r.rn <= 5
                GROUP BY r.day, r.dow
                ORDER BY r.dow, r.day
            """)
            raw_clicks_by_weekday_rows = cur.fetchall()

            # (B.2) Raw clicks — last 12 ISO weeks (HTML defaults to 8w view; user can switch 4w/8w/12w).
            cur.execute(f"""
                WITH clicks AS (
                    SELECT DATE_TRUNC('week', "Date"::date)::date AS w, issue_name
                    FROM {S}."Campaigns_Clicks"
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
                    COUNT(c.w) FILTER (
                        WHERE c.issue_name NOT ILIKE '%sunday spotlight%'
                    )                                                        AS clicks_no_ss,
                    (w.week_start = DATE_TRUNC('week', CURRENT_DATE)::date)  AS is_current
                FROM weeks w
                LEFT JOIN clicks c ON c.w = w.week_start
                GROUP BY w.week_start
                ORDER BY w.week_start
            """)
            raw_clicks_weekly_rows = cur.fetchall()

            # (B.3) Raw clicks — last 6 calendar months
            cur.execute(f"""
                WITH clicks AS (
                    SELECT DATE_TRUNC('month', "Date"::date)::date AS m, issue_name
                    FROM {S}."Campaigns_Clicks"
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
                    COUNT(c.m) FILTER (
                        WHERE c.issue_name NOT ILIKE '%sunday spotlight%'
                    )                                                          AS clicks_no_ss,
                    (m.month_start = DATE_TRUNC('month', CURRENT_DATE)::date)  AS is_current
                FROM months m
                LEFT JOIN clicks c ON c.m = m.month_start
                GROUP BY m.month_start
                ORDER BY m.month_start
            """)
            raw_clicks_monthly_rows = cur.fetchall()

    if not all_rows:
        result = {"data_as_of": today.isoformat(), "error": "no qualifying campaigns found"}
        commit_to_github(json.dumps(result, indent=2, default=str))
        return {"statusCode": 200, "body": "no data"}

    # ── Determine current and previous week ─────────────────────────
    # Current week = ISO week of the most recent launched campaign
    latest_date = max(r["sent_date"] for r in all_rows)
    cur_mon, cur_sun = week_bounds(latest_date)
    prev_mon = cur_mon - timedelta(days=7)
    prev_sun = cur_sun - timedelta(days=7)

    cur_week_rows  = [r for r in all_rows if cur_mon  <= r["sent_date"] <= cur_sun]
    prev_week_rows = [r for r in all_rows if prev_mon <= r["sent_date"] <= prev_sun]

    # ── Week-over-week summary ───────────────────────────────────────
    cur_agg  = aggregate_campaigns(cur_week_rows)
    prev_agg = aggregate_campaigns(prev_week_rows)
    wow_summary = build_comparison(cur_agg, prev_agg)

    # ── Day-over-day (each weekday: this week vs prev week) ─────────
    day_comparison = []
    for iso_dow in range(1, 8):  # 1=Mon .. 7=Sun
        c_rows = [r for r in cur_week_rows  if int(r["isodow"]) == iso_dow]
        p_rows = [r for r in prev_week_rows if int(r["isodow"]) == iso_dow]
        day_name = WEEKDAY_NAMES[iso_dow - 1]
        cur_day_date  = (cur_mon  + timedelta(days=iso_dow - 1)).isoformat()
        prev_day_date = (prev_mon + timedelta(days=iso_dow - 1)).isoformat()
        day_comparison.append({
            "weekday":        day_name,
            "current_date":   cur_day_date,
            "previous_date":  prev_day_date,
            "comparison":     build_comparison(aggregate_campaigns(c_rows), aggregate_campaigns(p_rows)),
            "current_campaigns":  _campaign_detail(c_rows),
            "previous_campaigns": _campaign_detail(p_rows),
        })

    # ── Full campaign-level detail for both weeks ────────────────────
    cur_detail  = _campaign_detail(cur_week_rows)
    prev_detail = _campaign_detail(prev_week_rows)

    # ── KPI chart arrays (last 12 weeks) ────────────────────────────
    weekly_trend = _build_weekly_trend(all_rows, weeks=12)

    M = {
        "data_as_of":       today.isoformat(),
        "launch_cutoff":    launch_cutoff.isoformat(),
        "current_week": {
            "start": cur_mon.isoformat(),
            "end":   cur_sun.isoformat(),
        },
        "previous_week": {
            "start": prev_mon.isoformat(),
            "end":   prev_sun.isoformat(),
        },
        "week_over_week":    wow_summary,
        "day_comparison":    day_comparison,
        "current_campaigns": cur_detail,
        "previous_campaigns": prev_detail,
        "weekly_trend":      weekly_trend,
        # Click Analysis trends (Section A — campaign aggregates)
        "campaign_clicks_weekly": {
            "labels":       [str(r["label"])      for r in campaign_weekly_rows],
            "week_starts":  [str(r["week_start"]) for r in campaign_weekly_rows],
            "clicks":       [safe_int(r["clicks"])       for r in campaign_weekly_rows],
            "unique_opens": [safe_int(r["unique_opens"]) for r in campaign_weekly_rows],
            "campaigns":    [safe_int(r["campaigns"])    for r in campaign_weekly_rows],
            "is_current":   [bool(r["is_current"])       for r in campaign_weekly_rows],
        },
        "campaign_clicks_monthly": {
            "labels":       [str(r["label"])       for r in campaign_monthly_rows],
            "month_starts": [str(r["month_start"]) for r in campaign_monthly_rows],
            "clicks":       [safe_int(r["clicks"])       for r in campaign_monthly_rows],
            "unique_opens": [safe_int(r["unique_opens"]) for r in campaign_monthly_rows],
            "campaigns":    [safe_int(r["campaigns"])    for r in campaign_monthly_rows],
            "is_current":   [bool(r["is_current"])       for r in campaign_monthly_rows],
        },
        "campaign_clicks_same_weekday": {
            "labels":       [str(r["label"]) for r in campaign_same_weekday_rows],
            "days":         [str(r["day"])   for r in campaign_same_weekday_rows],
            "clicks":       [safe_int(r["clicks"])       for r in campaign_same_weekday_rows],
            "unique_opens": [safe_int(r["unique_opens"]) for r in campaign_same_weekday_rows],
            "campaigns":    [safe_int(r["campaigns"])    for r in campaign_same_weekday_rows],
            "is_current":   [bool(r["is_current"])       for r in campaign_same_weekday_rows],
        },
        "campaign_clicks_same_dom": {
            "labels":       [str(r["label"]) for r in campaign_same_dom_rows],
            "days":         [str(r["day"])   for r in campaign_same_dom_rows],
            "clicks":       [safe_int(r["clicks"])       for r in campaign_same_dom_rows],
            "unique_opens": [safe_int(r["unique_opens"]) for r in campaign_same_dom_rows],
            "campaigns":    [safe_int(r["campaigns"])    for r in campaign_same_dom_rows],
            "is_current":   [bool(r["is_current"])       for r in campaign_same_dom_rows],
        },
        # Click Analysis trends (Section B — raw click events).
        # clicks_no_ss = same count but excluding rows whose issue_name
        # matches Sunday Spotlight; powers the "Include Sunday Spotlight"
        # toggle on the Click Analysis tab.
        "raw_clicks_same_weekday": {
            "labels":       [str(r["label"]) for r in raw_clicks_same_weekday_rows],
            "days":         [str(r["day"])   for r in raw_clicks_same_weekday_rows],
            "clicks":       [safe_int(r["clicks"])       for r in raw_clicks_same_weekday_rows],
            "clicks_no_ss": [safe_int(r["clicks_no_ss"]) for r in raw_clicks_same_weekday_rows],
            "is_current":   [bool(r["is_current"])       for r in raw_clicks_same_weekday_rows],
        },
        # Per-weekday raw click history (last 5 of each weekday).
        "raw_clicks_by_weekday": (lambda rows: {
            dow: {
                "labels":       [str(r["label"]) for r in rows if str(r["dow"]).strip() == dow],
                "days":         [str(r["day"])   for r in rows if str(r["dow"]).strip() == dow],
                "clicks":       [safe_int(r["clicks"])       for r in rows if str(r["dow"]).strip() == dow],
                "clicks_no_ss": [safe_int(r["clicks_no_ss"]) for r in rows if str(r["dow"]).strip() == dow],
                "is_current":   [bool(r["is_current"])       for r in rows if str(r["dow"]).strip() == dow],
            }
            for dow in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        })(raw_clicks_by_weekday_rows),
        "raw_clicks_weekly": {
            "labels":       [str(r["label"])      for r in raw_clicks_weekly_rows],
            "week_starts":  [str(r["week_start"]) for r in raw_clicks_weekly_rows],
            "clicks":       [safe_int(r["clicks"])       for r in raw_clicks_weekly_rows],
            "clicks_no_ss": [safe_int(r["clicks_no_ss"]) for r in raw_clicks_weekly_rows],
            "is_current":   [bool(r["is_current"])       for r in raw_clicks_weekly_rows],
        },
        "raw_clicks_monthly": {
            "labels":       [str(r["label"])       for r in raw_clicks_monthly_rows],
            "month_starts": [str(r["month_start"]) for r in raw_clicks_monthly_rows],
            "clicks":       [safe_int(r["clicks"])       for r in raw_clicks_monthly_rows],
            "clicks_no_ss": [safe_int(r["clicks_no_ss"]) for r in raw_clicks_monthly_rows],
            "is_current":   [bool(r["is_current"])       for r in raw_clicks_monthly_rows],
        },
    }

    payload = json.dumps(M, indent=2, default=str)
    commit_to_github(payload)
    logger.info("Done — comparison JSON committed.")
    return {"statusCode": 200, "body": "ok"}


def _campaign_detail(rows):
    return [
        {
            "name":        str(r["Campaign Name"] or ""),
            "subject":     str(r["Subject"] or ""),
            "sent_date":   r["sent_date"].isoformat() if hasattr(r["sent_date"], "isoformat") else str(r["sent_date"]),
            "weekday":     WEEKDAY_NAMES[int(r["isodow"]) - 1],
            "recipients":  safe_int(r["Recipients"]),
            "unique_opens": safe_int(r["UniqueOpened"]),
            "clicks":      safe_int(r["Clicks"]),
            "unsubs":      safe_int(r["Unsubscribed"]),
            "open_rate":   round(safe_float(r["UOpenRate"]), 2),
            "click_rate":  round(safe_float(r["UClickRate"]), 2),
            "url":         str(r.get("URL") or ""),
        }
        for r in rows
    ]


def _build_weekly_trend(all_rows, weeks=12):
    """Last N ISO weeks — aggregate per week for trend charts."""
    if not all_rows:
        return {"labels": [], "campaigns": [], "avg_open_rate": [], "avg_click_rate": [], "recipients": [], "clicks": []}

    latest = max(r["sent_date"] for r in all_rows)
    cur_mon, _ = week_bounds(latest)

    labels, n_camps, open_rates, click_rates, recipients, clicks = [], [], [], [], [], []

    for i in range(weeks - 1, -1, -1):
        w_mon = cur_mon - timedelta(weeks=i)
        w_sun = w_mon + timedelta(days=6)
        w_rows = [r for r in all_rows if w_mon <= r["sent_date"] <= w_sun]
        agg = aggregate_campaigns(w_rows)
        labels.append(w_mon.strftime("%b %d"))
        n_camps.append(agg["campaigns"] if agg else 0)
        open_rates.append(agg["avg_open_rate"] if agg else None)
        click_rates.append(agg["avg_click_rate"] if agg else None)
        recipients.append(agg["recipients"] if agg else 0)
        clicks.append(agg["clicks"] if agg else 0)

    return {
        "labels":          labels,
        "campaigns":       n_camps,
        "avg_open_rate":   open_rates,
        "avg_click_rate":  click_rates,
        "recipients":      recipients,
        "clicks":          clicks,
    }


if __name__ == "__main__":
    # Local test: set COMMIT_TO_GITHUB=false and DB env vars
    import pprint
    result = lambda_handler({}, None)
    pprint.pprint(result)
