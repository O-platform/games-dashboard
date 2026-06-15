
"""
SuperAge + AllHealthy + Ageist Games Dashboard — campaigns_life.json Refresh Lambda
=================================================================================

What this Lambda does:
  1. Reads SuperAge games campaign data from SuperAge RDS tables.
  2. Reads AllHealthy games campaign data from AllHealthy RDS tables.
  3. Reads Ageist games campaign data from ageist.ageist_campaigns and
     ageist.ageist_campaign_articles.
  4. Writes one JSON file to Cloudflare R2:
       game-campaigns/campaigns_life.json

AllHealthy unique-clicker logic:
  Unique game clickers come from allhealthy_contact_clicks (email + data columns).
  data::text is matched against the games URL patterns.
  mailing_name joins to newsletter_campaigns.title.
  Falls back to campaign_top_links.unique_clicks if the table does not exist.

  Total Opens KPI uses the `opens` column (non-unique).
  Rate calculations (open_rate, game_ctr) continue to use unique_opens.

Ageist logic:
  Ageist campaigns are included when their campaign article/link rows contain
  one of the Games URL patterns, including:
    - games.superage.com
    - superage.com/games
    - encoded redirect values such as superage.com%2Fgames

Required env vars:
  DB_SECRET_ARN      — SuperAge / shared DB Secrets Manager ARN
  AH_DB_SECRET_ARN   — AllHealthy Secrets Manager ARN, optional if AH_DB_* vars are set
  R2_SECRET_ARN      — Secrets Manager ARN; secret must carry keys:
                       account_id, access_key_id, secret_access_key, bucket_name

Optional env vars:
  R2_FILE_PATH       default: game-campaigns/campaigns_life.json
  WRITE_TO_R2        default: true; set false for local/test runs

  SuperAge:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_SSLMODE
    SA_SCHEMA        default: superage
    GAME_URL_PATTERNS default: %o.superage.com/r?dest=games.superage.com%,%games.superage.com%
    SA_GAMES_PATTERN is still accepted for backward compatibility, but GAME_URL_PATTERNS is preferred.

  AllHealthy:
    AH_DB_HOST, AH_DB_PORT, AH_DB_NAME, AH_DB_USER, AH_DB_PASSWORD, AH_DB_SSLMODE
    AH_GAMES_PATTERNS  default uses GAME_URL_PATTERNS
    AH_CONTACT_CLICKS_TABLE  default: allhealthy_contact_clicks

  Ageist:
    AGEIST_DB_SECRET_ARN, AG_DB_SECRET_ARN, or fallback to DB_SECRET_ARN
    AGEIST_DB_HOST, AGEIST_DB_PORT, AGEIST_DB_NAME, AGEIST_DB_USER, AGEIST_DB_PASSWORD, AGEIST_DB_SSLMODE
    AGEIST_SCHEMA          default: ageist
    AGEIST_CAMPAIGNS_TABLE default: ageist_campaigns
    AGEIST_ARTICLES_TABLE  default: ageist_campaign_articles
    AGEIST_GAMES_PATTERNS  comma-separated patterns; default uses GAME_URL_PATTERNS
"""



import json
import logging
import os
from datetime import datetime, timezone

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_sa_secret_cache = None
_ah_secret_cache = None
_ageist_secret_cache = None
_r2_secret_cache = None
_r2_client_cache = None

R2_FILE_PATH = os.environ.get("R2_FILE_PATH", "game-campaigns/campaigns_life.json")
WRITE_TO_R2  = os.environ.get("WRITE_TO_R2", "true").strip().lower() not in {"0", "false", "no"}

SA_SCHEMA = os.environ.get("SA_SCHEMA", "superage")

GAME_URL_PATTERNS = [
    x.strip()
    for x in os.environ.get(
        "GAME_URL_PATTERNS",
        "%o.superage.com/r?dest=games.superage.com%,%games.superage.com%",
    ).split(",")
    if x.strip()
]

# Backward-compatible brand-specific overrides.
SA_GAMES_PATTERNS = [
    x.strip()
    for x in os.environ.get(
        "SA_GAMES_PATTERNS",
        os.environ.get("SA_GAMES_PATTERN", ",".join(GAME_URL_PATTERNS)),
    ).split(",")
    if x.strip()
]

AH_GAMES_PATTERNS = [
    x.strip()
    for x in os.environ.get(
        "AH_GAMES_PATTERNS",
        ",".join(GAME_URL_PATTERNS),
    ).split(",")
    if x.strip()
]

AH_CONTACT_CLICKS_TABLE = os.environ.get("AH_CONTACT_CLICKS_TABLE", "allhealthy_contact_clicks")

AGEIST_SCHEMA = os.environ.get("AGEIST_SCHEMA", "ageist")
AGEIST_CAMPAIGNS_TABLE = os.environ.get("AGEIST_CAMPAIGNS_TABLE", "ageist_campaigns")
AGEIST_ARTICLES_TABLE = os.environ.get("AGEIST_ARTICLES_TABLE", "ageist_campaign_articles")
AGEIST_CLICKS_TABLE = os.environ.get("AGEIST_CLICKS_TABLE", "ageist_clicks")
AGEIST_GAMES_PATTERNS = [
    x.strip()
    for x in os.environ.get(
        "AGEIST_GAMES_PATTERNS",
        ",".join(GAME_URL_PATTERNS),
    ).split(",")
    if x.strip()
]

HB_SCHEMA           = os.environ.get("HB_SCHEMA",           "optimism")
HB_CAMPAIGNS_TABLE  = os.environ.get("HB_CAMPAIGNS_TABLE",  "healthbrief_campaigns_metrics")
HB_CONTACT_TABLE    = os.environ.get("HB_CONTACT_TABLE",    "healthbrief_contact_activity")
HB_GAMES_PATTERNS   = [
    x.strip()
    for x in os.environ.get(
        "HB_GAMES_PATTERNS",
        ",".join(GAME_URL_PATTERNS),
    ).split(",")
    if x.strip()
]


# ─────────────────────────────────────────────────────────────
# R2
# ─────────────────────────────────────────────────────────────

def _get_r2_secret():
    global _r2_secret_cache
    if _r2_secret_cache is not None:
        return _r2_secret_cache
    client = boto3.client(
        "secretsmanager",
        region_name=os.environ.get("AWS_REGION", "us-west-1"),
    )
    _r2_secret_cache = json.loads(
        client.get_secret_value(SecretId=os.environ["R2_SECRET_ARN"])["SecretString"]
    )
    return _r2_secret_cache


def _get_r2_client():
    global _r2_client_cache
    if _r2_client_cache is not None:
        return _r2_client_cache, _get_r2_secret()["bucket_name"]
    s = _get_r2_secret()
    endpoint = f"https://{s['account_id']}.r2.cloudflarestorage.com"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=s["access_key_id"],
        aws_secret_access_key=s["secret_access_key"],
        region_name="auto",
    )
    _r2_client_cache = client
    return client, s["bucket_name"]


def write_to_r2(content: str):
    if not WRITE_TO_R2:
        logger.warning("WRITE_TO_R2=false — skipping upload.")
        return {"uploaded": False, "reason": "dry_run"}
    try:
        client, bucket = _get_r2_client()
        client.put_object(
            Bucket=bucket,
            Key=R2_FILE_PATH,
            Body=content.encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("R2 upload OK — bucket=%s key=%s", bucket, R2_FILE_PATH)
        return {"uploaded": True, "bucket": bucket, "key": R2_FILE_PATH}
    except Exception as e:
        logger.error("R2 upload failed: %s", e)
        return {"uploaded": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# DB connections
# ─────────────────────────────────────────────────────────────

def _get_secret_by_arn(arn, cache_name):
    cached = globals()[cache_name]
    if cached is not None:
        return cached

    client = boto3.client(
        "secretsmanager",
        region_name=os.environ.get("AWS_REGION", "us-west-1"),
    )
    secret = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    globals()[cache_name] = secret
    return secret


def _get_secret_from_env(arn_env_key, cache_name):
    arn = os.environ[arn_env_key]
    return _get_secret_by_arn(arn, cache_name)


def sa_connection():
    s = _get_secret_from_env("DB_SECRET_ARN", "_sa_secret_cache")
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", s["host"]),
        port=int(os.environ.get("DB_PORT", s.get("port", 5432))),
        dbname=os.environ.get("DB_NAME", s["dbname"]),
        user=os.environ.get("DB_USER", s.get("username") or s.get("user")),
        password=s["password"],
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=30,
    )


def ah_connection():
    arn = os.environ.get("AH_DB_SECRET_ARN", "")
    if arn:
        s = _get_secret_by_arn(arn, "_ah_secret_cache")
        host = os.environ.get("AH_DB_HOST", s["host"])
        port = int(os.environ.get("AH_DB_PORT", s.get("port", 5432)))
        dbname = os.environ.get("AH_DB_NAME", s["dbname"])
        user = os.environ.get("AH_DB_USER", s.get("username") or s.get("user"))
        password = os.environ.get("AH_DB_PASSWORD", s["password"])
    else:
        host = os.environ["AH_DB_HOST"]
        port = int(os.environ.get("AH_DB_PORT", 5432))
        dbname = os.environ["AH_DB_NAME"]
        user = os.environ["AH_DB_USER"]
        password = os.environ["AH_DB_PASSWORD"]

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=os.environ.get("AH_DB_SSLMODE", "require"),
        connect_timeout=30,
    )


def ageist_connection():
    arn = (
        os.environ.get("AGEIST_DB_SECRET_ARN")
        or os.environ.get("AG_DB_SECRET_ARN")
        or os.environ.get("DB_SECRET_ARN")
    )

    if arn:
        s = _get_secret_by_arn(arn, "_ageist_secret_cache")
        host = os.environ.get("AGEIST_DB_HOST", s["host"])
        port = int(os.environ.get("AGEIST_DB_PORT", s.get("port", 5432)))
        dbname = os.environ.get("AGEIST_DB_NAME", s["dbname"])
        user = os.environ.get("AGEIST_DB_USER", s.get("username") or s.get("user"))
        password = os.environ.get("AGEIST_DB_PASSWORD", s["password"])
    else:
        host = os.environ["AGEIST_DB_HOST"]
        port = int(os.environ.get("AGEIST_DB_PORT", 5432))
        dbname = os.environ["AGEIST_DB_NAME"]
        user = os.environ["AGEIST_DB_USER"]
        password = os.environ["AGEIST_DB_PASSWORD"]

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=os.environ.get("AGEIST_DB_SSLMODE", "require"),
        connect_timeout=30,
    )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def pct(n, d, decimals=2):
    try:
        n = float(n or 0)
        d = float(d or 0)
        return f"{round(100.0 * n / d, decimals):.{decimals}f}%" if d else "0.00%"
    except Exception:
        return "0.00%"


def fmt(n):
    try:
        return f"{int(n or 0):,}"
    except Exception:
        return str(n)


def safe_int(v, default=0):
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


def safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def rate_to_percent(value):
    """
    Handles both stored fractions like 0.4833 and stored percentages like 48.33.
    """
    val = safe_float(value, 0.0)
    if 0 <= val <= 1:
        return val * 100
    return val


def sql_ident(name):
    import re
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return f'"{name}"'


def _table_exists(cur, schema_name, table_name):
    cur.execute("SELECT to_regclass(%s) AS rel", (f"{schema_name}.{table_name}",))
    row = cur.fetchone()
    return bool(row and row.get("rel"))


# ─────────────────────────────────────────────────────────────
# SuperAge queries
# ─────────────────────────────────────────────────────────────

def query_superage():
    S = sql_ident(SA_SCHEMA)
    PATS = SA_GAMES_PATTERNS
    conn = sa_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(f"""
            SELECT DISTINCT issue_name
            FROM {S}."Campaigns_Clicks"
            WHERE "URL" ILIKE ANY(%s) AND issue_name IS NOT NULL
        """, (PATS,))
        life_issues = [r["issue_name"] for r in cur.fetchall()]

        cur.execute(f"""
            SELECT issue_name,
                   COUNT(*) AS clicks,
                   COUNT(DISTINCT "EmailAddress ") AS unique_clicks
            FROM {S}."Campaigns_Clicks"
            WHERE "URL" ILIKE ANY(%s) AND issue_name IS NOT NULL
            GROUP BY issue_name
        """, (PATS,))
        click_rows = {r["issue_name"]: r for r in cur.fetchall()}

        campaign_meta = []
        if life_issues:
            cur.execute(f"""
                SELECT "Campaign Name", "Sent Date ", "Subject", "URL",
                       "Recipients", "TotalOpened", "UniqueOpened", "Clicks",
                       "Unsubscribed", "UOpenRate", "UClickRate"
                FROM {S}."Campaigns"
                WHERE "Campaign Name" = ANY(%s)
                ORDER BY "Sent Date " DESC NULLS LAST
            """, (life_issues,))
            campaign_meta = cur.fetchall()

        cur.execute(f"""
            SELECT DATE_TRUNC('month', "Date")::date AS month,
                   COUNT(*) AS clicks,
                   COUNT(DISTINCT "EmailAddress ") AS unique_clicks
            FROM {S}."Campaigns_Clicks"
            WHERE "URL" ILIKE ANY(%s) AND "Date" IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (PATS,))
        monthly = cur.fetchall()

        funnel_row = None
        if life_issues:
            cur.execute(f"""
                SELECT COALESCE(SUM("Recipients"),   0) AS recipients,
                       COALESCE(SUM("TotalOpened"),  0) AS total_opened,
                       COALESCE(SUM("UniqueOpened"), 0) AS unique_opens,
                       COALESCE(SUM("Clicks"),       0) AS total_clicks,
                       COALESCE(SUM("Unsubscribed"), 0) AS unsubs
                FROM {S}."Campaigns"
                WHERE "Campaign Name" = ANY(%s)
            """, (life_issues,))
            funnel_row = cur.fetchone()

        cur.execute(f"""
            SELECT COUNT(*) AS total_clicks,
                   COUNT(DISTINCT "EmailAddress ") AS unique_clicks
            FROM {S}."Campaigns_Clicks"
            WHERE "URL" ILIKE ANY(%s)
        """, (PATS,))
        life_totals = cur.fetchone()

    finally:
        cur.close()
        conn.close()

    return _build_sa_section(campaign_meta, click_rows, monthly, funnel_row, life_totals)


def _build_sa_section(campaign_meta, click_rows, monthly, funnel_row, life_totals):
    recipients   = safe_int(funnel_row["recipients"])   if funnel_row else 0
    total_opens  = safe_int(funnel_row["total_opened"]) if funnel_row else 0
    unique_opens = safe_int(funnel_row["unique_opens"]) if funnel_row else 0
    total_clicks = safe_int(funnel_row["total_clicks"]) if funnel_row else 0
    game_clicks  = safe_int(life_totals["total_clicks"])  if life_totals else 0
    game_unique  = safe_int(life_totals["unique_clicks"]) if life_totals else 0

    camps = []
    for r in campaign_meta:
        name = str(r["Campaign Name"])
        cr   = click_rows.get(name, {})
        gc   = safe_int(cr.get("clicks", 0))
        gu   = safe_int(cr.get("unique_clicks", 0))
        rec  = safe_int(r["Recipients"])
        op   = safe_int(r["TotalOpened"])
        uo   = safe_int(r["UniqueOpened"])
        orf  = safe_float(r["UOpenRate"])
        crf  = safe_float(r["UClickRate"])
        camps.append({
            "name":             name,
            "subject":          str(r["Subject"] or ""),
            "url":              str(r.get("URL") or ""),
            "sent_date":        str(r["Sent Date "])[:10] if r["Sent Date "] else "",
            "recipients":       rec,
            "recipients_fmt":   fmt(rec),
            "unique_opens":     op,
            "unique_opens_fmt": fmt(op),
            "open_rate":        f"{orf:.2f}%",
            "open_rate_f":      orf,
            "total_clicks":     safe_int(r["Clicks"]),
            "click_rate":       f"{crf:.2f}%",
            "click_rate_f":     crf,
            "game_clicks":      gc,
            "game_clicks_fmt":  fmt(gc),
            "game_unique":      gu,
            "game_unique_fmt":  fmt(gu),
            "game_ctr":         pct(gu, uo),
            "game_ctr_f":       round(100.0 * gu / uo, 2) if uo else 0,
            "unsubs":           safe_int(r["Unsubscribed"]),
        })

    trend = list(reversed(camps))[-30:]
    return {
        "kpis": {
            "total_campaigns":         len(camps),
            "total_recipients":        recipients,
            "total_recipients_fmt":    fmt(recipients),
            "total_unique_opens":      total_opens,
            "total_unique_opens_fmt":  fmt(total_opens),
            "total_game_clicks":       game_clicks,
            "total_game_clicks_fmt":   fmt(game_clicks),
            "total_game_unique":       game_unique,
            "total_game_unique_fmt":   fmt(game_unique),
            "avg_open_rate":           pct(unique_opens, recipients),
            "game_ctr":                pct(game_unique, unique_opens),
        },
        "funnel": {
            "labels": ["Recipients", "Total Opens", "Any Clicks", "Games Clicks", "Unique Games Clickers"],
            "data":   [recipients, total_opens, total_clicks, game_clicks, game_unique],
            "pcts":   [
                "100%",
                pct(total_opens,  recipients),
                pct(total_clicks, recipients),
                pct(game_clicks,  recipients),
                pct(game_unique,  recipients),
            ],
        },
        "campaigns": camps,
        "campaign_trend": {
            "labels":      [c["name"][:30] for c in trend],
            "game_clicks": [c["game_clicks"] for c in trend],
            "game_unique": [c["game_unique"] for c in trend],
            "open_rates":  [c["open_rate_f"] for c in trend],
        },
        "monthly_trend": {
            "labels":        [str(r["month"]) for r in monthly],
            "clicks":        [safe_int(r["clicks"]) for r in monthly],
            "unique_clicks": [safe_int(r["unique_clicks"]) for r in monthly],
        },
    }


# ─────────────────────────────────────────────────────────────
# AllHealthy queries
# ─────────────────────────────────────────────────────────────

def query_allhealthy():
    PATS = AH_GAMES_PATTERNS
    conn = ah_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT DISTINCT issue_name
            FROM public.campaign_top_links
            WHERE url ILIKE ANY(%s)
            ORDER BY issue_name
        """, (PATS,))
        game_issues = [r["issue_name"] for r in cur.fetchall()]

        click_rows = {}

        campaign_meta = []
        if game_issues:
            cur.execute("""
                SELECT title, sent_at, targeted, delivered,
                       opens, unique_opens, unique_clicks,
                       unsubscribes, open_rate_pct, ctr_pct
                FROM public.newsletter_campaigns
                WHERE title = ANY(%s)
                ORDER BY sent_at DESC NULLS LAST
            """, (game_issues,))
            campaign_meta = [dict(r) for r in cur.fetchall()]

        monthly = []

        funnel_row = None
        if game_issues:
            cur.execute("""
                SELECT COALESCE(SUM(targeted),      0) AS recipients,
                       COALESCE(SUM(opens),         0) AS total_opens,
                       COALESCE(SUM(unique_opens),  0) AS unique_opens,
                       COALESCE(SUM(unique_clicks), 0) AS total_clicks,
                       COALESCE(SUM(unsubscribes),  0) AS unsubs
                FROM public.newsletter_campaigns
                WHERE title = ANY(%s)
            """, (game_issues,))
            funnel_row = cur.fetchone()

        game_totals = None

        ah_contact_available = _table_exists(cur, "public", AH_CONTACT_CLICKS_TABLE)
        ah_campaign_uniques  = {}
        ah_total_unique      = None

        if ah_contact_available:
            cur.execute(f"""
                SELECT mailing_name,
                       COUNT(*) AS clicks
                FROM public.{AH_CONTACT_CLICKS_TABLE}
                WHERE data::text ILIKE ANY(%s)
                  AND (mailing_name IS NULL OR mailing_name NOT ILIKE '%%[TEST]%%')
                  AND mailing_name IS NOT NULL
                  AND bot = 'No'
                GROUP BY mailing_name
            """, (PATS,))
            click_rows = {r["mailing_name"]: r for r in cur.fetchall()}

            cur.execute(f"""
                SELECT COUNT(*) AS total_clicks
                FROM public.{AH_CONTACT_CLICKS_TABLE}
                WHERE data::text ILIKE ANY(%s)
                  AND (mailing_name IS NULL OR mailing_name NOT ILIKE '%%[TEST]%%')
                  AND bot = 'No'
            """, (PATS,))
            game_totals = cur.fetchone()

            cur.execute(f"""
                SELECT DATE_TRUNC('month', event_timestamp)::date AS month,
                       COUNT(*) AS clicks
                FROM public.{AH_CONTACT_CLICKS_TABLE}
                WHERE data::text ILIKE ANY(%s)
                  AND event_timestamp IS NOT NULL
                  AND (mailing_name IS NULL OR mailing_name NOT ILIKE '%%[TEST]%%')
                  AND bot = 'No'
                GROUP BY 1
                ORDER BY 1
            """, (PATS,))
            monthly = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT mailing_name,
                       COUNT(DISTINCT email) AS game_unique_dedup
                FROM public.{AH_CONTACT_CLICKS_TABLE}
                WHERE data::text ILIKE ANY(%s)
                  AND mailing_name IS NOT NULL
                  AND mailing_name NOT ILIKE '%%[TEST]%%'
                  AND bot = 'No'
                GROUP BY mailing_name
            """, (PATS,))
            ah_campaign_uniques = {
                r["mailing_name"]: safe_int(r["game_unique_dedup"])
                for r in cur.fetchall()
            }

            cur.execute(f"""
                SELECT COUNT(DISTINCT email) AS total_game_unique_dedup
                FROM public.{AH_CONTACT_CLICKS_TABLE}
                WHERE data::text ILIKE ANY(%s)
                  AND (mailing_name IS NULL OR mailing_name NOT ILIKE '%%[TEST]%%')
                  AND bot = 'No'
            """, (PATS,))
            row = cur.fetchone()
            ah_total_unique = safe_int(row["total_game_unique_dedup"]) if row else 0

            for r in campaign_meta:
                r["game_unique_dedup"] = ah_campaign_uniques.get(str(r["title"]), 0)

            cur.execute(f"""
                SELECT DATE_TRUNC('month', event_timestamp)::date AS month,
                       COUNT(DISTINCT cc.email) AS unique_clicks
                FROM public.{AH_CONTACT_CLICKS_TABLE} cc
                WHERE cc.data::text ILIKE ANY(%s)
                  AND cc.event_timestamp IS NOT NULL
                  AND (cc.mailing_name IS NULL OR cc.mailing_name NOT ILIKE '%%[TEST]%%')
                  AND cc.bot = 'No'
                GROUP BY 1
                ORDER BY 1
            """, (PATS,))
            monthly_unique_map = {
                str(r["month"]): safe_int(r["unique_clicks"])
                for r in cur.fetchall()
            }
            for row in monthly:
                row["unique_clicks"] = monthly_unique_map.get(str(row["month"]), 0)

        else:
            logger.warning(
                "AllHealthy contact_clicks table '%s' not found — "
                "falling back to campaign_top_links unique_clicks.",
                AH_CONTACT_CLICKS_TABLE,
            )
            cur.execute("""
                SELECT issue_name,
                       SUM(unique_clicks) AS unique_clicks
                FROM (
                    SELECT issue_name, issue_date, unique_clicks,
                           ROW_NUMBER() OVER (
                               PARTITION BY issue_name, issue_date ORDER BY clicks DESC
                           ) AS rn
                    FROM public.campaign_top_links
                    WHERE url ILIKE ANY(%s)
                ) t WHERE rn = 1
                GROUP BY issue_name
            """, (PATS,))
            fallback_uniques = {r["issue_name"]: safe_int(r["unique_clicks"]) for r in cur.fetchall()}
            for r in campaign_meta:
                r["game_unique_dedup"] = fallback_uniques.get(str(r["title"]), 0)

            cur.execute("""
                SELECT DATE_TRUNC('month', issue_date)::date AS month,
                       SUM(unique_clicks) AS unique_clicks
                FROM (
                    SELECT issue_date, unique_clicks,
                           ROW_NUMBER() OVER (
                               PARTITION BY issue_name, issue_date ORDER BY clicks DESC
                           ) AS rn
                    FROM public.campaign_top_links
                    WHERE url ILIKE ANY(%s)
                ) t WHERE rn = 1
                GROUP BY 1 ORDER BY 1
            """, (PATS,))
            fallback_monthly_uniques = {
                str(r["month"]): safe_int(r["unique_clicks"])
                for r in cur.fetchall()
            }
            for row in monthly:
                row["unique_clicks"] = fallback_monthly_uniques.get(str(row["month"]), 0)

    finally:
        cur.close()
        conn.close()

    return _build_ah_section(
        campaign_meta, click_rows, monthly, funnel_row, game_totals,
        ah_total_unique=ah_total_unique,
        ah_contact_available=ah_contact_available,
    )


def _build_ah_section(campaign_meta, click_rows, monthly, funnel_row, game_totals,
                       ah_total_unique=None, ah_contact_available=False):
    recipients = safe_int(funnel_row["recipients"])   if funnel_row else 0
    total_opens  = safe_int(funnel_row["total_opens"])  if funnel_row else 0
    unique_opens = safe_int(funnel_row["unique_opens"]) if funnel_row else 0
    total_clicks = safe_int(funnel_row["total_clicks"]) if funnel_row else 0
    game_clicks = safe_int(game_totals["total_clicks"]) if game_totals else 0

    if ah_contact_available and ah_total_unique is not None:
        game_unique    = safe_int(ah_total_unique)
        unique_method  = "contact_clicks_deduped_across_campaigns"
    else:
        game_unique   = sum(safe_int(r.get("game_unique_dedup", 0)) for r in campaign_meta)
        unique_method = "campaign_top_links_unique_clicks_fallback"
    camps = []
    for r in campaign_meta:
        name = str(r["title"])
        cr   = click_rows.get(name, {})
        gc   = safe_int(cr.get("clicks", 0))
        gu   = safe_int(r.get("game_unique_dedup", 0))
        rec  = safe_int(r["targeted"])
        op   = safe_int(r.get("opens", 0))
        uo   = safe_int(r["unique_opens"])
        orf  = safe_float(r["open_rate_pct"])
        crf  = safe_float(r["ctr_pct"])
        camps.append({
            "name":             name,
            "sent_date":        str(r["sent_at"])[:10] if r["sent_at"] else "",
            "recipients":       rec,
            "recipients_fmt":   fmt(rec),
            "unique_opens":     op,
            "unique_opens_fmt": fmt(op),
            "open_rate":        f"{orf:.2f}%",
            "open_rate_f":      orf,
            "total_clicks":     safe_int(r["unique_clicks"]),
            "click_rate":       f"{crf:.2f}%",
            "click_rate_f":     crf,
            "game_clicks":      gc,
            "game_clicks_fmt":  fmt(gc),
            "game_unique":      gu,
            "game_unique_fmt":  fmt(gu),
            "game_ctr":         pct(gu, uo),
            "game_ctr_f":       round(100.0 * gu / uo, 2) if uo else 0,
            "unsubs":           safe_int(r["unsubscribes"]),
        })

    trend = list(reversed(camps))[-30:]
    return {
        "kpis": {
            "total_campaigns":        len(camps),
            "total_recipients":       recipients,
            "total_recipients_fmt":   fmt(recipients),
            "total_unique_opens":     total_opens,
            "total_unique_opens_fmt": fmt(total_opens),
            "total_game_clicks":      game_clicks,
            "total_game_clicks_fmt":  fmt(game_clicks),
            "total_game_unique":      game_unique,
            "total_game_unique_fmt":  fmt(game_unique),
            "avg_open_rate":          pct(unique_opens, recipients),
            "game_ctr":               pct(game_unique, unique_opens),
            "unique_clicker_method":  unique_method,
            "ah_contact_available":   ah_contact_available,
        },
        "funnel": {
            "labels": ["Recipients", "Total Opens", "Any Clicks", "Games Clicks", "Unique Games Clickers"],
            "data":   [recipients, total_opens, total_clicks, game_clicks, game_unique],
            "pcts":   [
                "100%",
                pct(total_opens,  recipients),
                pct(total_clicks, recipients),
                pct(game_clicks,  recipients),
                pct(game_unique,  recipients),
            ],
        },
        "campaigns": camps,
        "campaign_trend": {
            "labels":      [c["name"][:30] for c in trend],
            "game_clicks": [c["game_clicks"] for c in trend],
            "game_unique": [c["game_unique"] for c in trend],
            "open_rates":  [c["open_rate_f"] for c in trend],
        },
        "monthly_trend": {
            "labels":        [str(r["month"]) for r in monthly],
            "clicks":        [safe_int(r["clicks"]) for r in monthly],
            "unique_clicks": [safe_int(r.get("unique_clicks", 0)) for r in monthly],
        },
    }


# ─────────────────────────────────────────────────────────────
# Ageist queries
# ─────────────────────────────────────────────────────────────

def _ageist_table(name):
    return f"{sql_ident(AGEIST_SCHEMA)}.{sql_ident(name)}"


def _ageist_games_filter(alias="a"):
    where = f"COALESCE({alias}.final_url, '') ILIKE ANY(%s)"
    params = (AGEIST_GAMES_PATTERNS,)
    return where, params


def _subscriber_key_sql(alias="ck"):
    return f"NULLIF(LOWER(TRIM({alias}.email_address)), '')"


def query_ageist():
    campaign_table = _ageist_table(AGEIST_CAMPAIGNS_TABLE)
    article_table  = _ageist_table(AGEIST_ARTICLES_TABLE)
    clicks_table   = _ageist_table(AGEIST_CLICKS_TABLE)

    article_where, article_params = _ageist_games_filter("a")
    clicks_where,  clicks_params  = _ageist_games_filter("ck")

    conn = ageist_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    raw_clicks_available = False
    raw_campaign_uniques = {}
    raw_total_unique     = None
    raw_monthly_uniques  = {}

    try:
        raw_clicks_available = _table_exists(cur, AGEIST_SCHEMA, AGEIST_CLICKS_TABLE)

        cur.execute(f"""
            WITH game_articles AS (
                SELECT
                    a.campaign_id,
                    SUM(COALESCE(a.total_clicks,  0)) AS game_clicks,
                    SUM(COALESCE(a.unique_clicks, 0)) AS game_unique_summary,
                    MAX(a.last_click) AS last_game_click
                FROM {article_table} a
                WHERE {article_where}
                GROUP BY a.campaign_id
            )
            SELECT
                c.campaign_id,
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
            FROM {campaign_table} c
            JOIN game_articles ga ON ga.campaign_id = c.campaign_id
            ORDER BY c.send_time DESC NULLS LAST
        """, article_params)
        campaign_meta = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT
                DATE_TRUNC('month', COALESCE(a.campaign_send_time, c.send_time))::date AS month,
                SUM(COALESCE(a.total_clicks,  0)) AS clicks,
                SUM(COALESCE(a.unique_clicks, 0)) AS unique_clicks_summary
            FROM {article_table} a
            JOIN {campaign_table} c ON c.campaign_id = a.campaign_id
            WHERE {article_where}
              AND COALESCE(a.campaign_send_time, c.send_time) IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """, article_params)
        monthly = [dict(r) for r in cur.fetchall()]

        if raw_clicks_available:
            subscriber_key = _subscriber_key_sql("ck")

            cur.execute(f"""
                SELECT
                    ck.campaign_id,
                    COUNT(DISTINCT {subscriber_key}) AS game_unique_dedup
                FROM {clicks_table} ck
                WHERE {clicks_where}
                  AND {subscriber_key} IS NOT NULL
                GROUP BY ck.campaign_id
            """, clicks_params)
            raw_campaign_uniques = {
                r["campaign_id"]: safe_int(r["game_unique_dedup"])
                for r in cur.fetchall()
            }

            cur.execute(f"""
                SELECT
                    COUNT(DISTINCT {subscriber_key}) AS total_game_unique_dedup
                FROM {clicks_table} ck
                WHERE {clicks_where}
                  AND {subscriber_key} IS NOT NULL
            """, clicks_params)
            row = cur.fetchone()
            raw_total_unique = safe_int(row["total_game_unique_dedup"]) if row else 0

            cur.execute(f"""
                SELECT
                    DATE_TRUNC('month', ck.campaign_send_time)::date AS month,
                    COUNT(DISTINCT {subscriber_key}) AS unique_clicks
                FROM {clicks_table} ck
                WHERE {clicks_where}
                  AND ck.campaign_send_time IS NOT NULL
                  AND {subscriber_key} IS NOT NULL
                GROUP BY 1
                ORDER BY 1
            """, clicks_params)
            raw_monthly_uniques = {
                str(r["month"]): safe_int(r["unique_clicks"])
                for r in cur.fetchall()
            }

            for row in campaign_meta:
                row["game_unique_dedup"] = raw_campaign_uniques.get(row["campaign_id"], 0)

            for row in monthly:
                month_key = str(row["month"])
                row["unique_clicks"] = raw_monthly_uniques.get(month_key, 0)

        else:
            logger.warning(
                "Ageist raw clicks table %s.%s does not exist. Falling back to article summary unique_clicks.",
                AGEIST_SCHEMA,
                AGEIST_CLICKS_TABLE,
            )
            for row in campaign_meta:
                row["game_unique_dedup"] = safe_int(row.get("game_unique_summary"))
            for row in monthly:
                row["unique_clicks"] = safe_int(row.get("unique_clicks_summary"))

    finally:
        cur.close()
        conn.close()

    return _build_ageist_section(
        campaign_meta,
        monthly,
        raw_total_unique=raw_total_unique,
        raw_clicks_available=raw_clicks_available,
    )


def _build_ageist_section(campaign_meta, monthly, raw_total_unique=None, raw_clicks_available=False):
    recipients      = sum(safe_int(r.get("emails_sent"))   for r in campaign_meta)
    total_opens_sum = sum(safe_int(r.get("total_opens", 0)) for r in campaign_meta)
    unique_opens    = sum(safe_int(r.get("unique_opens"))    for r in campaign_meta)
    total_clicks    = sum(safe_int(r.get("total_clicks"))    for r in campaign_meta)
    game_clicks     = sum(safe_int(r.get("game_clicks"))     for r in campaign_meta)

    if raw_clicks_available and raw_total_unique is not None:
        game_unique   = safe_int(raw_total_unique)
        unique_method = "raw_clicks_deduped_across_campaigns"
    else:
        game_unique   = sum(safe_int(r.get("game_unique_summary")) for r in campaign_meta)
        unique_method = "article_summary_unique_clicks_fallback"

    camps = []
    for r in campaign_meta:
        name = str(r.get("campaign_title") or r.get("campaign_id") or "")
        rec  = safe_int(r.get("emails_sent"))
        op   = safe_int(r.get("total_opens", 0))
        uo   = safe_int(r.get("unique_opens"))
        gc   = safe_int(r.get("game_clicks"))
        gu   = safe_int(r.get("game_unique_dedup") if raw_clicks_available else r.get("game_unique_summary"))
        orf  = rate_to_percent(r.get("open_rate"))
        crf  = rate_to_percent(r.get("click_rate"))
        sent = r.get("send_time")

        camps.append({
            "name":             name,
            "subject":          str(r.get("subject_line") or ""),
            "url":              "",
            "sent_date":        str(sent)[:10] if sent else "",
            "recipients":       rec,
            "recipients_fmt":   fmt(rec),
            "unique_opens":     op,
            "unique_opens_fmt": fmt(op),
            "open_rate":        f"{orf:.2f}%",
            "open_rate_f":      orf,
            "total_clicks":     safe_int(r.get("total_clicks")),
            "click_rate":       f"{crf:.2f}%",
            "click_rate_f":     crf,
            "game_clicks":      gc,
            "game_clicks_fmt":  fmt(gc),
            "game_unique":      gu,
            "game_unique_fmt":  fmt(gu),
            "game_ctr":         pct(gu, uo),
            "game_ctr_f":       round(100.0 * gu / uo, 2) if uo else 0,
            "unsubs":           safe_int(r.get("unsubscribed")),
        })

    trend = list(reversed(camps))[-30:]
    return {
        "kpis": {
            "total_campaigns":        len(camps),
            "total_recipients":       recipients,
            "total_recipients_fmt":   fmt(recipients),
            "total_unique_opens":     total_opens_sum,
            "total_unique_opens_fmt": fmt(total_opens_sum),
            "total_game_clicks":      game_clicks,
            "total_game_clicks_fmt":  fmt(game_clicks),
            "total_game_unique":      game_unique,
            "total_game_unique_fmt":  fmt(game_unique),
            "avg_open_rate":          pct(unique_opens, recipients),
            "game_ctr":               pct(game_unique, unique_opens),
            "unique_clicker_method":  unique_method,
            "raw_clicks_available":   raw_clicks_available,
        },
        "funnel": {
            "labels": ["Recipients", "Total Opens", "Any Clicks", "Games Clicks", "Unique Games Clickers"],
            "data":   [recipients, total_opens_sum, total_clicks, game_clicks, game_unique],
            "pcts":   [
                "100%",
                pct(total_opens_sum, recipients),
                pct(total_clicks,    recipients),
                pct(game_clicks,     recipients),
                pct(game_unique,     recipients),
            ],
        },
        "campaigns": camps,
        "campaign_trend": {
            "labels":      [c["name"][:30] for c in trend],
            "game_clicks": [c["game_clicks"] for c in trend],
            "game_unique": [c["game_unique"] for c in trend],
            "open_rates":  [c["open_rate_f"] for c in trend],
        },
        "monthly_trend": {
            "labels":        [str(r["month"]) for r in monthly],
            "clicks":        [safe_int(r["clicks"]) for r in monthly],
            "unique_clicks": [safe_int(r.get("unique_clicks", 0)) for r in monthly],
        },
    }


# ─────────────────────────────────────────────────────────────
# HealthBrief queries  (optimism schema, same DB as SuperAge)
# ─────────────────────────────────────────────────────────────

def query_healthbrief():
    S_C  = f'"{HB_SCHEMA}"."{HB_CAMPAIGNS_TABLE}"'
    S_A  = f'"{HB_SCHEMA}"."{HB_CONTACT_TABLE}"'
    PATS = HB_GAMES_PATTERNS
    conn = sa_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(f"""
            SELECT DISTINCT mailing_name
            FROM {S_A}
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND mailing_name IS NOT NULL
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
        """, (PATS,))
        game_names = [r["mailing_name"] for r in cur.fetchall()]

        campaign_meta = []
        funnel_row    = None
        if game_names:
            cur.execute(f"""
                SELECT mailing_id, mailing_name, schedule_dt,
                       targeted, sent, opens, unique_opens, clicks, unique_clicks,
                       unsubscribes, hard_bounces, soft_bounces, complaints,
                       open_rate, click_rate, ctr
                FROM {S_C}
                WHERE mailing_name = ANY(%s)
                  AND is_test = '0'
                ORDER BY schedule_dt DESC NULLS LAST
            """, (game_names,))
            campaign_meta = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT COALESCE(SUM(targeted),     0) AS recipients,
                       COALESCE(SUM(opens),        0) AS total_opens,
                       COALESCE(SUM(unique_opens), 0) AS unique_opens,
                       COALESCE(SUM(clicks),       0) AS total_clicks
                FROM {S_C}
                WHERE mailing_name = ANY(%s)
                  AND is_test = '0'
            """, (game_names,))
            funnel_row = cur.fetchone()

        cur.execute(f"""
            SELECT mailing_name, COUNT(*) AS game_clicks
            FROM {S_A}
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND mailing_name IS NOT NULL
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
            GROUP BY mailing_name
        """, (PATS,))
        game_clicks_map = {r["mailing_name"]: safe_int(r["game_clicks"]) for r in cur.fetchall()}

        cur.execute(f"""
            SELECT mailing_name, COUNT(DISTINCT email) AS game_unique
            FROM {S_A}
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND mailing_name IS NOT NULL
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
            GROUP BY mailing_name
        """, (PATS,))
        game_unique_map = {r["mailing_name"]: safe_int(r["game_unique"]) for r in cur.fetchall()}

        cur.execute(f"""
            SELECT COUNT(DISTINCT email) AS total_game_unique
            FROM {S_A}
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
        """, (PATS,))
        row = cur.fetchone()
        total_game_unique = safe_int(row["total_game_unique"]) if row else 0

        cur.execute(f"""
            SELECT DATE_TRUNC('month', timestamp)::date AS month,
                   COUNT(*) AS clicks,
                   COUNT(DISTINCT email) AS unique_clicks
            FROM {S_A}
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND timestamp IS NOT NULL
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
            GROUP BY 1
            ORDER BY 1
        """, (PATS,))
        monthly = [dict(r) for r in cur.fetchall()]

    finally:
        cur.close()
        conn.close()

    return _build_hb_section(
        campaign_meta, funnel_row, game_clicks_map, game_unique_map, total_game_unique, monthly
    )


def _build_hb_section(campaign_meta, funnel_row, game_clicks_map, game_unique_map, total_game_unique, monthly):
    recipients   = safe_int(funnel_row["recipients"])   if funnel_row else 0
    total_opens  = safe_int(funnel_row["total_opens"])  if funnel_row else 0
    unique_opens = safe_int(funnel_row["unique_opens"]) if funnel_row else 0
    total_clicks = safe_int(funnel_row["total_clicks"]) if funnel_row else 0
    game_clicks  = sum(game_clicks_map.values())
    game_unique  = total_game_unique

    camps = []
    for r in campaign_meta:
        name = str(r["mailing_name"])
        gc   = game_clicks_map.get(name, 0)
        gu   = game_unique_map.get(name, 0)
        rec  = safe_int(r["targeted"])
        op   = safe_int(r.get("opens", 0))
        uo   = safe_int(r.get("unique_opens", 0))
        # open_rate / click_rate are already stored as percentages (e.g. 65.7, 7.86)
        orf  = safe_float(r.get("open_rate"))
        crf  = safe_float(r.get("click_rate"))
        sent = r.get("schedule_dt")
        camps.append({
            "name":             name,
            "sent_date":        str(sent)[:10] if sent else "",
            "recipients":       rec,
            "recipients_fmt":   fmt(rec),
            "unique_opens":     op,
            "unique_opens_fmt": fmt(op),
            "open_rate":        f"{orf:.2f}%",
            "open_rate_f":      orf,
            "total_clicks":     safe_int(r.get("clicks", 0)),
            "click_rate":       f"{crf:.2f}%",
            "click_rate_f":     crf,
            "game_clicks":      gc,
            "game_clicks_fmt":  fmt(gc),
            "game_unique":      gu,
            "game_unique_fmt":  fmt(gu),
            "game_ctr":         pct(gu, uo),
            "game_ctr_f":       round(100.0 * gu / uo, 2) if uo else 0,
            "unsubs":           safe_int(r.get("unsubscribes", 0)),
        })

    trend = list(reversed(camps))[-30:]
    return {
        "kpis": {
            "total_campaigns":        len(camps),
            "total_recipients":       recipients,
            "total_recipients_fmt":   fmt(recipients),
            "total_unique_opens":     total_opens,
            "total_unique_opens_fmt": fmt(total_opens),
            "total_game_clicks":      game_clicks,
            "total_game_clicks_fmt":  fmt(game_clicks),
            "total_game_unique":      game_unique,
            "total_game_unique_fmt":  fmt(game_unique),
            "avg_open_rate":          pct(unique_opens, recipients),
            "game_ctr":               pct(game_unique, unique_opens),
            "unique_clicker_method":  "contact_activity_deduped_across_campaigns",
        },
        "funnel": {
            "labels": ["Recipients", "Total Opens", "Any Clicks", "Games Clicks", "Unique Games Clickers"],
            "data":   [recipients, total_opens, total_clicks, game_clicks, game_unique],
            "pcts":   [
                "100%",
                pct(total_opens,  recipients),
                pct(total_clicks, recipients),
                pct(game_clicks,  recipients),
                pct(game_unique,  recipients),
            ],
        },
        "campaigns": camps,
        "campaign_trend": {
            "labels":      [c["name"][:30] for c in trend],
            "game_clicks": [c["game_clicks"] for c in trend],
            "game_unique": [c["game_unique"] for c in trend],
            "open_rates":  [c["open_rate_f"] for c in trend],
        },
        "monthly_trend": {
            "labels":        [str(r["month"]) for r in monthly],
            "clicks":        [safe_int(r["clicks"]) for r in monthly],
            "unique_clicks": [safe_int(r.get("unique_clicks", 0)) for r in monthly],
        },
    }


# ─────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    now_utc = datetime.now(timezone.utc)
    try:
        import zoneinfo
        est = now_utc.astimezone(zoneinfo.ZoneInfo("America/New_York"))
        last_updated = est.strftime("%b %-d, %Y %I:%M %p EST")
    except Exception:
        last_updated = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    logger.info("campaigns_life Lambda starting — r2_key=%s", R2_FILE_PATH)

    sa_data, ah_data, ageist_data, hb_data = {}, {}, {}, {}
    errors = []

    try:
        sa_data = query_superage()
        logger.info("SuperAge OK — campaigns=%d", len(sa_data.get("campaigns", [])))
    except Exception as e:
        logger.exception("SuperAge query failed")
        errors.append({"source": "superage", "error": str(e)})

    try:
        ah_data = query_allhealthy()
        logger.info("AllHealthy OK — campaigns=%d", len(ah_data.get("campaigns", [])))
    except Exception as e:
        logger.exception("AllHealthy query failed")
        errors.append({"source": "allhealthy", "error": str(e)})

    try:
        ageist_data = query_ageist()
        logger.info("Ageist OK — campaigns=%d", len(ageist_data.get("campaigns", [])))
    except Exception as e:
        logger.exception("Ageist query failed")
        errors.append({"source": "ageist", "error": str(e)})

    try:
        hb_data = query_healthbrief()
        logger.info("HealthBrief OK — campaigns=%d", len(hb_data.get("campaigns", [])))
    except Exception as e:
        logger.exception("HealthBrief query failed")
        errors.append({"source": "healthbrief", "error": str(e)})

    output = {
        "last_updated": last_updated,
        "superage":     sa_data,
        "allhealthy":   ah_data,
        "ageist":       ageist_data,
        "healthbrief":  hb_data,
    }

    if errors:
        output["errors"] = errors

    body      = json.dumps(output, indent=2, default=str)
    r2_result = write_to_r2(body)

    return {
        "statusCode": 200 if not errors else 207,
        "body": json.dumps({
            "status":               "ok" if not errors else "partial_ok",
            "last_updated":         last_updated,
            "superage_campaigns":    len(sa_data.get("campaigns", [])),
            "allhealthy_campaigns":  len(ah_data.get("campaigns", [])),
            "ageist_campaigns":      len(ageist_data.get("campaigns", [])),
            "healthbrief_campaigns": len(hb_data.get("campaigns", [])),
            "r2":                   r2_result,
            "errors":               errors,
        }),
    }


