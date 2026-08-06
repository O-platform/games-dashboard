"""
AWE Course — Metrics Lambda
===========================

Builds the AWE Course dashboard JSON and uploads it to Cloudflare R2.

    RDS (clicks + waitlist + quiz)  ──►  this Lambda  ──►  R2  ──►  Worker  ──►  dashboard

What it measures
----------------
The AWE course link is:
    https://superage.com/awecourse/?utm_source=...&utm_medium=email&utm_campaign=...&oid=...
matched via `AWE_URL_PATTERNS` (default "%superage.com/awecourse%").

Sections of the output JSON:
  • kpis            — headline numbers
  • funnel          — Clicks → Waitlist → Buyers  (Buyers is a placeholder for now)
  • clicks          — distinct clickers, per-brand (SuperAge/AllHealthy/HealthBrief),
                      by campaign, and a combined daily trend
  • waitlist        — totals + UTM breakdowns (utm_source / utm_medium / utm_campaign)
                      + subscribe growth
  • buyers          — placeholder (no buyer source connected yet)
  • persona         — the priority section: clickers vs waitlisters vs quiz-takers,
                      every subscriber_quiz attribute, joined by email
  • quiz_uptake     — how many clickers / waitlisters took the longevity quiz

Click sources (all in the MAIN db):
  SuperAge    superage."Campaigns_Clicks"        URL   "URL",  email "EmailAddress ", camp issue_name,   date "Date"
  HealthBrief optimism.healthbrief_clicks           data,     email email,           camp mailing_name, date timestamp
  AllHealthy  optimism.allhealthy_clicks            data,     email email,           camp mailing_name, date timestamp
(AllHealthy/HealthBrief promos for AWE start later — they will simply read 0 until then.)

Environment variables
----------------------
    R2_SECRET_ARN   (required)  secret: account_id/access_key_id/secret_access_key/bucket_name
    R2_FILE_PATH    (optional)  default "awe-course/awe_course.json"
    WRITE_TO_R2     (optional)  "false" -> dry run, no upload
    DB_SECRET_ARN   (required)  main-db secret
    DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_SSLMODE  (optional overrides)
    SA_SCHEMA       (optional)  default "superage"
    AWE_URL_PATTERNS(optional)  comma list, default "%superage.com/awecourse%"
    AWE_MEMBERS_TABLE(optional) Circle members table (buyers), default superage.awe_course_members
    AWE_PRICE_USD   (optional)  lifetime price for revenue estimate, default 99
    SNS_TOPIC_ARN   (optional)  failure alerts
    AWS_REGION      (optional)  default us-west-1

Dependencies: boto3, psycopg2
"""

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SA_SCHEMA    = os.environ.get("SA_SCHEMA", "superage")
R2_FILE_PATH = os.environ.get("R2_FILE_PATH", "awe-course/awe_course.json")
WRITE_TO_R2  = os.environ.get("WRITE_TO_R2", "true").strip().lower() not in {"0", "false", "no"}
AWE_URL_PATTERNS = [
    x.strip() for x in os.environ.get("AWE_URL_PATTERNS", "%superage.com/awecourse%").split(",")
    if x.strip()
]
# Which click sources to scan (fallback path only — the matview already unions all
# four brands). AllHealthy/HealthBrief have NO AWE clicks until their promos launch,
# and their contact_activity tables are huge — scanning them for zero rows wastes
# minutes. Set AWE_CLICK_SOURCES=sa until AH/HB promos start (then sa,hb,ah,ag).
# Values: sa (SuperAge), hb (HealthBrief), ah (AllHealthy), ag (Ageist).
AWE_CLICK_SOURCES = [
    x.strip().lower() for x in os.environ.get("AWE_CLICK_SOURCES", "sa,hb,ah,ag").split(",")
    if x.strip()
]
# AWE course members synced from Circle (= buyers; $99 lifetime access).
AWE_MEMBERS_TABLE = os.environ.get("AWE_MEMBERS_TABLE", f"{SA_SCHEMA}.awe_course_members").strip()
AWE_PRICE_USD = int(os.environ.get("AWE_PRICE_USD", "99"))
# Landing-events table carrying oid + utm_* — buyer acquisition is attributed by
# joining members.oid -> awe_course_checkout_landing_events.oid (table may not exist yet).
AWE_LANDING_TABLE = os.environ.get("AWE_LANDING_TABLE", f"{SA_SCHEMA}.awe_course_checkout_landing_events").strip()
# Timestamp columns used for "latest click at/before purchase" attribution.
# Auto-detected from these candidates unless overridden by env.
AWE_PURCHASE_COL   = os.environ.get("AWE_PURCHASE_COL", "").strip()    # on members table
AWE_LANDING_TS_COL = os.environ.get("AWE_LANDING_TS_COL", "").strip()  # on landing table
_PURCHASE_TS_CANDIDATES = ["circle_created_at", "purchased_at", "purchase_date",
                           "order_date", "paid_at", "created_at"]
_LANDING_TS_CANDIDATES  = ["date", "created_at", "event_timestamp", "event_time",
                           "occurred_at", "click_time", "clicked_at", "timestamp", "ts"]
# Pre-computed purchaser acquisition matview (last-touch-before-purchase per buyer,
# with 'Unknown' baked in). When present the Lambda reads it instead of joining.
AWE_PURCHASER_MATVIEW = os.environ.get("AWE_PURCHASER_MATVIEW", f"{SA_SCHEMA}.mv_awe_purchaser_acquisition").strip()

# Pre-aggregated clicks matview (superage.mv_awe_clicks), refreshed daily by
# pg_cron. When present, the Lambda reads it instead of scanning the raw tables.
AWE_CLICKS_MATVIEW = os.environ.get("AWE_CLICKS_MATVIEW", f"{SA_SCHEMA}.mv_awe_clicks").strip()

# The 4 brands, in display order.
AWE_BRANDS = ["SuperAge", "AllHealthy", "HealthBrief", "Ageist"]

# Google Ads traffic (SuperAge). Two row types share the utm_campaign value:
#   - main-page landings  -> o_event IS NULL   -> top-of-funnel "Landed via Google Ads"
#   - checkout redirects   -> o_event = AWE_CHECKOUT_OEVENT -> "SuperAge Google Ads" checkout bar
# No oid on Google Ads rows, so these are non-unique event counts only. NOT folded
# into "SuperAge Website / Organic".
AWE_GOOGLE_ADS_CAMPAIGN = os.environ.get("AWE_GOOGLE_ADS_CAMPAIGN", "google_ads_awe").strip().lower()
AWE_GOOGLE_ADS_BUCKET   = "SuperAge Google Ads"
# The landing table holds two product_url types (never null), which is what the
# metrics use to tell a course-main-page landing from a checkout event:
#   course landing  -> product_url contains 'awecourse'  (https://superage.com/awecourse/)
#   checkout event  -> product_url contains 'checkout'   (https://super-age.circle.so/checkout/...)
AWE_LANDING_URL_MATCH   = os.environ.get("AWE_LANDING_URL_MATCH", "awecourse").strip().lower()
AWE_CHECKOUT_URL_MATCH  = os.environ.get("AWE_CHECKOUT_URL_MATCH", "checkout").strip().lower()
# Fallback discriminator only if product_url is ever missing from the table.
AWE_CHECKOUT_OEVENT     = os.environ.get("AWE_CHECKOUT_OEVENT", "awe_course_checkout_redirect").strip().lower()

# Static historical checkout-landing counts (from Google Analytics) for the
# window BEFORE the checkout table existed. Summed into the funnel + checkout
# chart. (bucket, day, count). SuperAge historical -> "SuperAge Campaigns".
# Documented in DELIVERY.md. The awe_course_checkout_landing_events table is used
# for everything it contains (Jul-27 overlap is accepted).
AWE_CHECKOUT_STATIC = [
    ("SuperAge Campaigns", "2026-07-19", 173),
    ("SuperAge Campaigns", "2026-07-20", 85),
    ("SuperAge Campaigns", "2026-07-21", 14),
    ("SuperAge Campaigns", "2026-07-22", 2),
    ("SuperAge Campaigns", "2026-07-23", 16),
    ("SuperAge Campaigns", "2026-07-24", 7),
    ("SuperAge Campaigns", "2026-07-25", 2),
    ("SuperAge Campaigns", "2026-07-26", 97),
    ("SuperAge Campaigns", "2026-07-27", 17),
    ("Ageist Campaigns",   "2026-07-23", 7),
]

_BRAND_LABEL = {"superage": "SuperAge", "allhealthy": "AllHealthy",
                "healthbrief": "HealthBrief", "ageist": "Ageist"}


def classify_acq(src, med, null_src="superage", null_med="email"):
    """(utm_source, utm_medium) -> acquisition bucket label. Null defaults vary by
    context (checkout: superage/website; waitlist/purchaser: superage/email)."""
    s = (src or "").strip().lower() or null_src
    m = (med or "").strip().lower() or null_med
    if s == "superage" and m == "website":
        return "SuperAge Website / Organic"
    if s in _BRAND_LABEL and m == "email":
        return _BRAND_LABEL[s] + " Campaigns"
    if s == "superage":
        return "SuperAge Website / Organic"
    if s in _BRAND_LABEL:
        return _BRAND_LABEL[s] + " Campaigns"
    return (src or "").strip() or "SuperAge Website / Organic"   # other source -> verbatim


def bucket_brand(bucket):
    b = (bucket or "").lower()
    for key, lbl in _BRAND_LABEL.items():
        if b.startswith(lbl.lower()):
            return lbl
    return "Other"

# Date floor when READING the rollup (earliest across sources = SuperAge's
# 2026-07-01). The rollup already enforces per-source floors (AH/HB from
# 2026-07-25), so this must NOT clip SuperAge. Set AWE_SINCE="" to disable.
AWE_SINCE = os.environ.get("AWE_SINCE", "2026-07-01").strip()

# Persona attributes to break down (from superage.subscriber_quiz). Categorical
# distributions; age is bucketed / averaged separately. Longevity score is
# intentionally excluded from the persona.
PERSONA_ATTRS = [
    "gender", "financial_situation", "education_level",
    "sleep_hours", "exercise_freq", "smoking_status",
    "alcohol_freq", "stress_impact",
]

_db_secret_cache = None
_r2_secret_cache = None
_r2_client_cache = None


# ─────────────────────────────────────────────────────────────
# Ops: SNS failure alert
# ─────────────────────────────────────────────────────────────

def _alert_failure(context, err):
    arn = os.environ.get("SNS_TOPIC_ARN")
    if not arn:
        logger.warning("SNS_TOPIC_ARN not set — skipping failure alert.")
        return
    try:
        fn = getattr(context, "function_name", "awe_metrics")
        boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-west-1")).publish(
            TopicArn=arn,
            Subject=f"[AWE] Metrics lambda FAILED: {fn}"[:99],
            Message=f"Lambda: {fn}\nError: {err}\n",
        )
        logger.info("Failure alert published to SNS.")
    except Exception as e:
        logger.error("Failed to publish SNS alert: %s", e)


# ─────────────────────────────────────────────────────────────
# R2 helpers (identical pattern to the other dashboard lambdas)
# ─────────────────────────────────────────────────────────────

def _get_r2_secret():
    global _r2_secret_cache
    if _r2_secret_cache is not None:
        return _r2_secret_cache
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    _r2_secret_cache = json.loads(
        client.get_secret_value(SecretId=os.environ["R2_SECRET_ARN"])["SecretString"]
    )
    return _r2_secret_cache


def _get_r2_client():
    global _r2_client_cache
    if _r2_client_cache is not None:
        return _r2_client_cache, _get_r2_secret()["bucket_name"]
    s = _get_r2_secret()
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{s['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=s["access_key_id"],
        aws_secret_access_key=s["secret_access_key"],
        region_name="auto",
    )
    _r2_client_cache = client
    return client, s["bucket_name"]


def write_to_r2(content: str):
    if not WRITE_TO_R2:
        logger.warning("WRITE_TO_R2=false -- skipping upload.")
        return {"uploaded": False, "reason": "dry_run"}
    client, bucket = _get_r2_client()
    client.put_object(Bucket=bucket, Key=R2_FILE_PATH,
                      Body=content.encode("utf-8"), ContentType="application/json")
    logger.info("R2 upload OK -- bucket=%s key=%s", bucket, R2_FILE_PATH)
    return {"uploaded": True, "bucket": bucket, "key": R2_FILE_PATH}


# ─────────────────────────────────────────────────────────────
# DB connection
# ─────────────────────────────────────────────────────────────

def _get_db_secret():
    global _db_secret_cache
    if _db_secret_cache is not None:
        return _db_secret_cache
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    _db_secret_cache = json.loads(
        client.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"]
    )
    logger.info("DB secret fetched from Secrets Manager.")
    return _db_secret_cache


def get_connection():
    s = _get_db_secret()
    return psycopg2.connect(
        host     = os.environ.get("DB_HOST",   s.get("host")),
        port     = int(os.environ.get("DB_PORT", s.get("port", 5432))),
        dbname   = os.environ.get("DB_NAME",   s.get("dbname")),
        user     = os.environ.get("DB_USER",   s.get("username")),
        password = s["password"],
        sslmode  = os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=30,
    )


# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────

def safe_int(v, default=0):
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


def fnum(v, nd=1):
    return round(float(v), nd) if v is not None else None


def table_exists(cur, regclass):
    cur.execute("SELECT to_regclass(%s) AS t", (regclass,))
    return cur.fetchone()["t"] is not None


def columns_of(cur, full_name):
    """Return the set of column names for a 'schema.table' (or 'table') name."""
    if "." in full_name:
        sch, tab = full_name.split(".", 1)
    else:
        sch, tab = "public", full_name
    sch, tab = sch.strip('"'), tab.strip('"')
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (sch, tab),
    )
    return {r["column_name"] for r in cur.fetchall()}


def find_col(cols, candidates):
    """First candidate present in the column set, else None."""
    return next((c for c in candidates if c in cols), None)


def _empty_utm(note):
    return {"available": False, "total": 0, "attributed": 0, "note": note,
            "by_utm_source": [], "by_utm_medium": [], "by_utm_campaign": []}


def waitlist_utm(cur, table, available):
    """UTM breakdown for the waitlist — ACTIVE only; null utm_source => superage,
    null utm_medium => email (a SuperAge signup), not 'Unknown'."""
    if not available or not table_exists(cur, table):
        return _empty_utm("Waitlist table not found.")
    cols = columns_of(cur, table)
    present = [c for c in ("utm_source", "utm_medium", "utm_campaign") if c in cols]
    if not present:
        return _empty_utm("No utm_* columns on the waitlist table.")
    active = "WHERE state ILIKE 'active'" if "state" in cols else ""
    defaults = {"utm_source": "superage", "utm_medium": "email", "utm_campaign": "Unknown"}
    out = _empty_utm(None)
    cur.execute(f"SELECT COUNT(*) AS n FROM {table} {active}")
    out["total"] = safe_int(cur.fetchone()["n"])
    out["available"] = True
    for col in present:
        cur.execute(f"""
            SELECT COALESCE(NULLIF(TRIM({col}),''),'{defaults[col]}') AS {col}, COUNT(*) AS count
            FROM {table} {active} GROUP BY 1 ORDER BY 2 DESC
        """)
        out["by_" + col] = [{col: r[col], "count": safe_int(r["count"])} for r in cur.fetchall()]
    return out


def buyers_utm(cur, members_table, landing_table, available):
    """
    Buyer acquisition via LAST-TOUCH-BEFORE-PURCHASE attribution.

    A buyer (oid) can have several landing clicks (e.g. one from AllHealthy and
    one from HealthBrief). We attribute each buyer to the LATEST landing click
    that happened AT OR BEFORE their purchase — clicks that occur AFTER the
    purchase are ignored (they didn't drive the sale). Every buyer is counted
    exactly once (by email, matching the Buyers KPI); a buyer with no qualifying
    pre-purchase click falls into 'Unknown'.

    Needs: oid on both tables, utm_* on landing, a purchase-timestamp on members
    and a click-timestamp on landing (auto-detected, or set via env).
    """
    if not available or not table_exists(cur, members_table):
        return _empty_utm("Members table not found.")

    # Preferred path: read the pre-computed purchaser-acquisition matview
    # (one row per buyer, utm already attributed + 'Unknown', attributed flag).
    if table_exists(cur, AWE_PURCHASER_MATVIEW):
        mv = AWE_PURCHASER_MATVIEW
        out = _empty_utm(None)
        cur.execute(f"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE attributed) AS attributed FROM {mv}")
        r = cur.fetchone()
        out["total"] = safe_int(r["total"])
        out["attributed"] = safe_int(r["attributed"])
        out["available"] = out["total"] > 0
        for col in ("utm_source", "utm_medium", "utm_campaign"):
            cur.execute(f"SELECT {col}, COUNT(*) AS count FROM {mv} GROUP BY 1 ORDER BY 2 DESC")
            out["by_" + col] = [{col: r[col], "count": safe_int(r["count"])} for r in cur.fetchall()]
        out["note"] = f"From {mv} (last click at/before purchase)."
        return out

    if not table_exists(cur, landing_table):
        return _empty_utm(f"Landing table {landing_table} not created yet — buyer UTMs will appear once it exists.")
    mcols, lcols = columns_of(cur, members_table), columns_of(cur, landing_table)
    if "oid" not in mcols or "oid" not in lcols:
        return _empty_utm("Need an 'oid' column on both the members and landing tables.")
    present = [c for c in ("utm_source", "utm_medium", "utm_campaign") if c in lcols]
    if not present:
        return _empty_utm("Landing table has no utm_* columns yet.")
    purchase_col = AWE_PURCHASE_COL or find_col(mcols, _PURCHASE_TS_CANDIDATES)
    click_col    = AWE_LANDING_TS_COL or find_col(lcols, _LANDING_TS_CANDIDATES)
    if not purchase_col or not click_col:
        miss = []
        if not purchase_col:
            miss.append(f"a purchase-timestamp column on {members_table} (e.g. circle_created_at)")
        if not click_col:
            miss.append(f"a click-timestamp column on {landing_table}")
        return _empty_utm("Time-based attribution needs " + " and ".join(miss) + ".")

    email_nb = "email IS NOT NULL AND TRIM(email) != ''"
    utm_sel = ", ".join(f"l.{c}" for c in present)
    # base = every buyer (distinct email); bp = one purchase time per buyer
    # (earliest); attributed = the latest click at/before that purchase time.
    cte = f"""
      WITH base AS (
        SELECT DISTINCT LOWER(TRIM(email)) AS email FROM {members_table} WHERE {email_nb}
      ),
      bp AS (
        SELECT DISTINCT ON (LOWER(TRIM(email)))
               LOWER(TRIM(email))     AS email,
               LOWER(TRIM(oid::text)) AS oid,
               {purchase_col}         AS purchased_at
        FROM {members_table}
        WHERE {email_nb} AND {purchase_col} IS NOT NULL AND NULLIF(TRIM(oid::text),'') IS NOT NULL
        ORDER BY LOWER(TRIM(email)), {purchase_col} ASC
      ),
      attributed AS (
        SELECT DISTINCT ON (bp.email) bp.email, {utm_sel}
        FROM bp
        JOIN {landing_table} l ON LOWER(TRIM(l.oid::text)) = bp.oid
        WHERE l.{click_col} IS NOT NULL
          AND l.{click_col} <= bp.purchased_at          -- click at/before purchase only
        ORDER BY bp.email, l.{click_col} DESC            -- latest such click wins
      )
    """
    out = _empty_utm(None)
    cur.execute(f"{cte} SELECT COUNT(*) AS n FROM base")
    out["total"] = safe_int(cur.fetchone()["n"])
    cur.execute(f"{cte} SELECT COUNT(*) AS n FROM attributed")
    out["attributed"] = safe_int(cur.fetchone()["n"])
    out["available"] = True
    for col in present:
        cur.execute(f"""
            {cte}
            SELECT COALESCE(NULLIF(TRIM(a.{col}),''),'Unknown') AS {col}, COUNT(*) AS count
            FROM base b LEFT JOIN attributed a ON a.email = b.email
            GROUP BY 1 ORDER BY 2 DESC
        """)
        out["by_" + col] = [{col: r[col], "count": safe_int(r["count"])} for r in cur.fetchall()]
    out["note"] = f"Last click at/before purchase (purchase={purchase_col}, click={click_col})."
    return out


def merge_utm(items):
    """Combine several entity UTM blocks into one ('all'): sum counts per value."""
    avail = [x for x in items if x.get("available")]
    if not avail:
        return _empty_utm("No acquisition data yet.")
    out = {"available": True, "total": sum(safe_int(x.get("total", 0)) for x in avail), "note": None,
           "by_utm_source": [], "by_utm_medium": [], "by_utm_campaign": []}
    for dim in ("by_utm_source", "by_utm_medium", "by_utm_campaign"):
        key = dim[len("by_"):]  # utm_source / utm_medium / utm_campaign
        agg = {}
        for x in avail:
            for row in x.get(dim, []):
                val = row.get(key) or row.get("val") or "Unknown"
                agg[val] = agg.get(val, 0) + safe_int(row.get("count", 0))
        out[dim] = [{key: v, "count": c} for v, c in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)]
    return out


# Each click source, guarded by existence so a missing/renamed AllHealthy table
# never aborts the run. Returns SQL fragments parameterised on AWE_URL_PATTERNS.
def click_source_sql(kind):
    """Return dict of SQL snippets for a click source, or None if kind unknown."""
    if kind == "sa":
        return dict(
            brand="SuperAge",
            regclass=f'{SA_SCHEMA}."Campaigns_Clicks"',
            table=f'{SA_SCHEMA}."Campaigns_Clicks"',
            match_col='"URL"',
            email='"EmailAddress "',
            campaign="issue_name",
            date='"Date"',
            extra="",
        )
    if kind == "hb":
        return dict(
            brand="HealthBrief",
            regclass="optimism.healthbrief_clicks",
            table="optimism.healthbrief_clicks",
            match_col="data",
            email="email",
            campaign="mailing_name",
            date='"timestamp"',
            extra="AND type = 'click' AND bot = 'No'",
        )
    if kind == "ah":
        return dict(
            brand="AllHealthy",
            regclass="optimism.allhealthy_clicks",
            table="optimism.allhealthy_clicks",
            match_col="data",
            email="email",
            campaign="mailing_name",
            date='"timestamp"',
            extra="AND type = 'click' AND bot = 'No'",
        )
    if kind in ("ag", "ageist"):
        # Ageist rows are pre-aggregated per member/link, so weight = click_count.
        # Matches on final_url with its own pattern (NOT the superage.com/awecourse
        # ones), and floors from 2026-07-01 like SuperAge.
        return dict(
            brand="Ageist",
            regclass="ageist.ageist_clicks",
            table="ageist.ageist_clicks",
            match_col="final_url",
            match_patterns=["%awecourse%"],
            email="email_address",
            campaign="campaign_title",
            date="first_seen_at",
            weight="COALESCE(click_count, 1)",
            extra="",
        )
    return None


# ── literal-pattern helpers ──────────────────────────────────
# We inline the AWE URL pattern and date as validated LITERALS (no bind params)
# so Postgres can match a partial index built on the same predicate. Everything
# inlined is validated against a strict allowlist first.
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9%._/:?=&\- ]+$")
_SAFE_DATE    = re.compile(r"^[0-9:\-. ]+$")


def _safe_pattern(p):
    if not _SAFE_PATTERN.match(p):
        raise ValueError(f"Unsafe AWE_URL_PATTERNS entry: {p!r}")
    return p


def _safe_date(s):
    if not _SAFE_DATE.match(s):
        raise ValueError(f"Unsafe AWE_SINCE value: {s!r}")
    return s


def build_match_clause(col, patterns=None):
    """(col LIKE 'p1' OR col LIKE 'p2' ...) with validated literal patterns.
    LIKE (case-sensitive) matches the matview; the AWE link is always lowercase.
    `patterns` defaults to AWE_URL_PATTERNS; sources with a different URL column
    (e.g. Ageist's final_url) pass their own."""
    ors = " OR ".join(f"{col} LIKE '{_safe_pattern(p)}'" for p in (patterns or AWE_URL_PATTERNS))
    return f"({ors})"


# ─────────────────────────────────────────────────────────────
# Persona — reusable block for any audience of emails
# ─────────────────────────────────────────────────────────────

def build_quiz_temp(cur):
    """
    Deduplicate superage.subscriber_quiz to one row per email ONCE into a temp
    table (_awe_quiz), indexed on email. Every persona query then joins this
    small indexed table instead of re-sorting the whole quiz table each time.
    (Longevity score is intentionally omitted — not shown in the persona.)
    """
    cur.execute("DROP TABLE IF EXISTS _awe_quiz")
    cur.execute(f"""
        CREATE TEMP TABLE _awe_quiz AS
        SELECT DISTINCT ON (LOWER(TRIM(sq.email)))
            LOWER(TRIM(sq.email)) AS email,
            sq.age, sq.gender, sq.financial_situation,
            sq.education_level, sq.sleep_hours,
            sq.smoking_status, sq.alcohol_freq, sq.stress_impact,
            COALESCE(sq.exercise_freq, sq.exercise_freq_male,
                     sq.exercise_freq_female, sq.exercise_freq_other) AS exercise_freq
        FROM {SA_SCHEMA}.subscriber_quiz sq
        WHERE sq.email IS NOT NULL AND TRIM(sq.email) != ''
        ORDER BY LOWER(TRIM(sq.email)), sq.created_at DESC
    """)
    cur.execute("CREATE INDEX ON _awe_quiz (email)")
    cur.execute("SELECT COUNT(*) AS n FROM _awe_quiz")
    logger.info("_awe_quiz built: %s deduped quiz rows", cur.fetchone()["n"])


def persona_for(cur, aud_table):
    """
    aud_table: name of a temp table with one column `email` (lower/trimmed).
    Joins it to _awe_quiz (already built) — all small, indexed, fast.
    aud_table is an internal constant name, never user input.
    """
    def rows(sql):
        cur.execute(sql)
        return cur.fetchall()

    total = safe_int(rows(f"SELECT COUNT(*) AS n FROM {aud_table}")[0]["n"])
    matched = safe_int(rows(
        f"SELECT COUNT(*) AS n FROM {aud_table} a JOIN _awe_quiz q ON a.email = q.email"
    )[0]["n"])

    avg = rows(f"""
        SELECT ROUND(AVG(q.age)::numeric,1) AS age
        FROM {aud_table} a JOIN _awe_quiz q ON a.email = q.email
    """)[0]

    age_buckets = [
        {"range": r["range"], "count": safe_int(r["count"])}
        for r in rows(f"""
            SELECT CASE
                     WHEN q.age < 45 THEN 'Under 45'
                     WHEN q.age < 55 THEN '45-54'
                     WHEN q.age < 65 THEN '55-64'
                     WHEN q.age < 75 THEN '65-74'
                     ELSE '75+'
                   END AS range, COUNT(*) AS count
            FROM {aud_table} a JOIN _awe_quiz q ON a.email = q.email
            WHERE q.age IS NOT NULL
            GROUP BY 1 ORDER BY MIN(q.age)
        """)
    ]

    attrs = {}
    for col in PERSONA_ATTRS:
        rr = rows(f"""
            SELECT CASE
                     WHEN q.{col} IS NULL OR TRIM(q.{col}::text) = '' THEN 'Not specified'
                     ELSE q.{col}::text
                   END AS val, COUNT(*) AS count
            FROM {aud_table} a JOIN _awe_quiz q ON a.email = q.email
            GROUP BY 1 ORDER BY 2 DESC
        """)
        attrs[col] = [{"val": r["val"], "count": safe_int(r["count"])} for r in rr]

    return {
        "audience_total": total,
        "matched_quiz":   matched,
        "match_rate_pct": round(100.0 * matched / total, 1) if total else None,
        "avg_age":        fnum(avg["age"]),
        "age_buckets":    age_buckets,
        "attributes":     attrs,
    }


# ─────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    try:
        return _run(event, context)
    except Exception as err:
        logger.exception("AWE metrics build failed")
        _alert_failure(context, err)
        raise


def _run(event, context):
    logger.info("AWE metrics starting -- r2_key=%s patterns=%s since=%s",
                R2_FILE_PATH, AWE_URL_PATTERNS, AWE_SINCE or "ALL")

    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    M = {}
    try:
        # ── build _awe_clicks ──
        # Preferred: read from the clicks matview (superage.mv_awe_clicks)
        # so the Lambda never scans the huge raw tables. Fallback: scan the raw
        # sources directly (only if the matview hasn't been created yet).
        # _awe_clicks holds one row per (brand,email,campaign,day) with click_count
        # so both unique (COUNT DISTINCT email) and non-unique (SUM click_count)
        # totals are exact whichever path is used.
        cur.execute("DROP TABLE IF EXISTS _awe_clicks")
        cur.execute("CREATE TEMP TABLE _awe_clicks (brand text, email text, campaign text, day date, click_count int)")

        if table_exists(cur, AWE_CLICKS_MATVIEW):
            logger.info("Using clicks matview %s (no raw scan).", AWE_CLICKS_MATVIEW)
            since_clause = f"WHERE click_date >= '{_safe_date(AWE_SINCE)}'" if AWE_SINCE else ""
            cur.execute(f"""
                INSERT INTO _awe_clicks (brand, email, campaign, day, click_count)
                SELECT brand, NULLIF(email,''), NULLIF(campaign,''), click_date, click_count
                FROM {AWE_CLICKS_MATVIEW} {since_clause}
            """)
            logger.info("loaded %s rows from matview", cur.rowcount)
        else:
            logger.warning("Matview %s not found -- falling back to raw table scan.", AWE_CLICKS_MATVIEW)
            for kind in ("sa", "hb", "ah", "ag"):
                if kind not in AWE_CLICK_SOURCES:
                    logger.info("Click source %s disabled via AWE_CLICK_SOURCES -- skipping.", kind)
                    continue
                s = click_source_sql(kind)
                if not table_exists(cur, s["regclass"]):
                    logger.warning("Click source %s (%s) not found -- skipping.", kind, s["regclass"])
                    continue
                conds = []
                if AWE_SINCE:
                    conds.append(f"{s['date']} >= '{_safe_date(AWE_SINCE)}'")
                conds.append(build_match_clause(s["match_col"], s.get("match_patterns")))
                where = "WHERE " + " AND ".join(conds) + " " + s["extra"]
                weight = s.get("weight", "1")
                cur.execute(f"""
                    INSERT INTO _awe_clicks (brand, email, campaign, day, click_count)
                    SELECT '{s['brand']}' AS brand,
                           NULLIF(LOWER(TRIM({s['email']}::text)), '') AS email,
                           {s['campaign']} AS campaign,
                           {s['date']}::date AS day,
                           {weight} AS click_count
                    FROM {s['table']} {where}
                """)
                logger.info("scanned %s (%s) since=%s -> %s AWE click rows",
                            kind, s["brand"], AWE_SINCE or "ALL", cur.rowcount)
        cur.execute("CREATE INDEX ON _awe_clicks (email)")

        # non-unique clicks = SUM(click_count); unique clickers = COUNT(DISTINCT email)
        cur.execute("""
            SELECT brand, SUM(click_count) AS clicks, COUNT(DISTINCT email) AS unique_clickers
            FROM _awe_clicks GROUP BY brand
        """)
        brow = {r["brand"]: r for r in cur.fetchall()}
        BRAND_ORDER = ["SuperAge", "AllHealthy", "HealthBrief", "Ageist"]
        brands_present = [b for b in BRAND_ORDER if b in brow] + [b for b in brow if b not in BRAND_ORDER]
        clicks_by_brand = [{
            "brand": b,
            "clicks": safe_int(brow[b]["clicks"]),
            "unique_clickers": safe_int(brow[b]["unique_clickers"]),
        } for b in brands_present]

        cur.execute("""
            SELECT brand, campaign, SUM(click_count) AS count
            FROM _awe_clicks WHERE campaign IS NOT NULL
            GROUP BY brand, campaign ORDER BY count DESC
        """)
        clicks_by_campaign = [{"brand": r["brand"], "campaign": r["campaign"],
                               "count": safe_int(r["count"])} for r in cur.fetchall()]

        cur.execute("SELECT day, SUM(click_count) AS count FROM _awe_clicks WHERE day IS NOT NULL GROUP BY day ORDER BY day")
        clicks_by_day = [{"day": str(r["day"]), "count": safe_int(r["count"])} for r in cur.fetchall()]

        # per-brand daily clicks (drives the brand-filtered Click Trend)
        cur.execute("SELECT brand, day, SUM(click_count) AS count FROM _awe_clicks "
                    "WHERE day IS NOT NULL GROUP BY brand, day ORDER BY day")
        clicks_day_by_brand = defaultdict(list)
        for r in cur.fetchall():
            clicks_day_by_brand[r["brand"]].append({"day": str(r["day"]), "count": safe_int(r["count"])})

        cur.execute("SELECT COALESCE(SUM(click_count),0) AS total, COUNT(DISTINCT email) AS distinct_clickers FROM _awe_clicks")
        r = cur.fetchone()
        total_clicks = safe_int(r["total"])
        distinct_clickers = safe_int(r["distinct_clickers"])

        M["clicks"] = {
            "distinct_clickers": distinct_clickers,
            "total_clicks":      total_clicks,
            "by_brand":          clicks_by_brand,
            "by_campaign":       clicks_by_campaign[:50],
            "by_day":            clicks_by_day,
            "sources_active":    brands_present,
            "source":            "matview" if table_exists(cur, AWE_CLICKS_MATVIEW) else "raw_scan",
        }

        # ── waitlist ──
        wl = {"total": 0, "by_utm_source": [], "by_utm_medium": [],
              "by_utm_campaign": [], "by_sub_level": [], "growth": []}
        waitlist_exists = table_exists(cur, f"{SA_SCHEMA}.awe_waitlist")
        # Only ACTIVE subscribers count — unsubscribed rows were email tests.
        WL_ACTIVE = "state ILIKE 'active'"
        if waitlist_exists:
            cur.execute(f"SELECT COUNT(*) AS n FROM {SA_SCHEMA}.awe_waitlist WHERE {WL_ACTIVE}")
            wl["total"] = safe_int(cur.fetchone()["n"])

            def wl_dist(col, key, default='Unknown'):
                cur.execute(f"""
                    SELECT COALESCE(NULLIF(TRIM({col}),''),'{default}') AS val, COUNT(*) AS count
                    FROM {SA_SCHEMA}.awe_waitlist WHERE {WL_ACTIVE}
                    GROUP BY 1 ORDER BY 2 DESC
                """)
                return [{key: r["val"], "count": safe_int(r["count"])} for r in cur.fetchall()]

            # Null utm on the waitlist => superage / email (a SuperAge signup), not Unknown.
            wl["by_utm_source"]   = wl_dist("utm_source",   "utm_source",   'superage')
            wl["by_utm_medium"]   = wl_dist("utm_medium",   "utm_medium",   'email')
            wl["by_utm_campaign"] = wl_dist("utm_campaign", "utm_campaign")
            wl["by_sub_level"]    = wl_dist("sub_level",    "sub_level")

            # Growth uses date_subscribed (never date_joined), ACTIVE only.
            cur.execute(f"""
                SELECT date_subscribed::date AS day, COUNT(*) AS count
                FROM {SA_SCHEMA}.awe_waitlist
                WHERE date_subscribed IS NOT NULL AND {WL_ACTIVE}
                GROUP BY 1 ORDER BY 1
            """)
            cum = 0
            for r in cur.fetchall():
                cum += safe_int(r["count"])
                wl["growth"].append({"day": str(r["day"]), "count": safe_int(r["count"]),
                                     "cumulative": cum})
        else:
            logger.warning("%s.awe_waitlist not found -- run the ingest lambda first.", SA_SCHEMA)
        M["waitlist"] = wl

        # ── buyers = AWE course members synced from Circle ──
        # One "buyer" = one distinct member email. Also exposes access-type/status
        # breakdowns, join-date growth, and crossover with waitlist + clickers.
        members_tbl = AWE_MEMBERS_TABLE
        buyers_total = 0
        buyers_placeholder = True
        buyers = {
            "total": 0, "placeholder": True, "price_usd": AWE_PRICE_USD,
            "by_access_type": [], "by_status": [], "growth": [],
            "waitlist_buyers": 0, "clicker_buyers": 0, "estimated_revenue_usd": 0,
        }
        # Real members = is_superage IS NOT TRUE (excludes ~7 internal/team accounts).
        members_exists = bool(members_tbl) and table_exists(cur, members_tbl)
        mcols = columns_of(cur, members_tbl) if members_exists else set()
        SUP    = " AND is_superage IS NOT TRUE"   if "is_superage" in mcols else ""
        SUP_M  = " AND m.is_superage IS NOT TRUE" if "is_superage" in mcols else ""
        NONBLANK = "email IS NOT NULL AND TRIM(email) != ''" + SUP
        if members_exists:
            buyers_placeholder = False
            cur.execute(f"SELECT COUNT(DISTINCT LOWER(TRIM(email))) AS n FROM {members_tbl} WHERE {NONBLANK}")
            buyers_total = safe_int(cur.fetchone()["n"])

            def m_dist(col, key):
                cur.execute(f"""
                    SELECT COALESCE(NULLIF(TRIM({col}::text),''),'Unknown') AS val,
                           COUNT(DISTINCT LOWER(TRIM(email))) AS count
                    FROM {members_tbl} WHERE {NONBLANK}
                    GROUP BY 1 ORDER BY 2 DESC
                """)
                return [{key: r["val"], "count": safe_int(r["count"])} for r in cur.fetchall()]
            buyers["by_access_type"] = m_dist("access_type", "access_type")
            buyers["by_status"]      = m_dist("status", "status")

            # join-date growth (Circle created_at) — the members-equivalent of a
            # buyer/purchase date; used for the funnel/trend, not waitlist dates.
            cur.execute(f"""
                SELECT circle_created_at::date AS day, COUNT(DISTINCT LOWER(TRIM(email))) AS count
                FROM {members_tbl}
                WHERE circle_created_at IS NOT NULL AND {NONBLANK}
                GROUP BY 1 ORDER BY 1
            """)
            cum = 0
            for r in cur.fetchall():
                cum += safe_int(r["count"])
                buyers["growth"].append({"day": str(r["day"]), "count": safe_int(r["count"]), "cumulative": cum})

            # crossover: how many members were on the waitlist / clicked the link
            if table_exists(cur, f"{SA_SCHEMA}.awe_waitlist"):
                cur.execute(f"""
                    SELECT COUNT(DISTINCT LOWER(TRIM(m.email))) AS n
                    FROM {members_tbl} m
                    JOIN {SA_SCHEMA}.awe_waitlist w ON LOWER(TRIM(m.email)) = LOWER(TRIM(w.email))
                    WHERE m.email IS NOT NULL AND TRIM(m.email) != ''{SUP_M}
                      AND w.{WL_ACTIVE}
                """)
                buyers["waitlist_buyers"] = safe_int(cur.fetchone()["n"])

            cur.execute(f"""
                SELECT COUNT(DISTINCT LOWER(TRIM(m.email))) AS n
                FROM {members_tbl} m
                JOIN _awe_clicks c ON LOWER(TRIM(m.email)) = c.email
                WHERE m.email IS NOT NULL AND TRIM(m.email) != ''{SUP_M}
            """)
            buyers["clicker_buyers"] = safe_int(cur.fetchone()["n"])

            buyers["estimated_revenue_usd"] = AWE_PRICE_USD * buyers_total
            logger.info("buyers (members) total=%s waitlist_buyers=%s clicker_buyers=%s",
                        buyers_total, buyers["waitlist_buyers"], buyers["clicker_buyers"])
        else:
            logger.warning("Members table %s not found -- buyers shown as placeholder.", members_tbl)

        buyers["total"] = buyers_total
        buyers["placeholder"] = buyers_placeholder
        buyers["note"] = None if not buyers_placeholder else f"Members table {members_tbl} not found."
        M["buyers"] = buyers

        # ── persona: build quiz + audience temp tables ONCE, then join ──
        build_quiz_temp(cur)

        # clickers audience = distinct emails already collected in _awe_clicks
        cur.execute("DROP TABLE IF EXISTS _aud_clickers")
        cur.execute("CREATE TEMP TABLE _aud_clickers AS SELECT DISTINCT email FROM _awe_clicks WHERE email IS NOT NULL")
        cur.execute("CREATE INDEX ON _aud_clickers (email)")

        cur.execute("DROP TABLE IF EXISTS _aud_waitlisters")
        if table_exists(cur, f"{SA_SCHEMA}.awe_waitlist"):
            cur.execute(f"""
                CREATE TEMP TABLE _aud_waitlisters AS
                SELECT DISTINCT LOWER(TRIM(email)) AS email
                FROM {SA_SCHEMA}.awe_waitlist
                WHERE email IS NOT NULL AND TRIM(email) != '' AND {WL_ACTIVE}
            """)
        else:
            cur.execute("CREATE TEMP TABLE _aud_waitlisters (email text)")
        cur.execute("CREATE INDEX ON _aud_waitlisters (email)")

        # buyers audience = distinct member emails (empty table if members missing)
        cur.execute("DROP TABLE IF EXISTS _aud_buyers")
        if not buyers_placeholder:
            cur.execute(f"""
                CREATE TEMP TABLE _aud_buyers AS
                SELECT DISTINCT LOWER(TRIM(email)) AS email FROM {members_tbl} WHERE {NONBLANK}
            """)
        else:
            cur.execute("CREATE TEMP TABLE _aud_buyers (email text)")
        cur.execute("CREATE INDEX ON _aud_buyers (email)")

        # "all" = anyone who clicked, waitlisted, or bought (deduped union)
        cur.execute("DROP TABLE IF EXISTS _aud_all")
        cur.execute("""
            CREATE TEMP TABLE _aud_all AS
            SELECT email FROM _aud_clickers
            UNION SELECT email FROM _aud_waitlisters
            UNION SELECT email FROM _aud_buyers
        """)
        cur.execute("CREATE INDEX ON _aud_all (email)")

        # Each audience is joined to the quiz, so distributions reflect people who
        # (clicked / waitlisted / bought / any) AND took the quiz. "all" is the
        # combined audience; the others break it down.
        persona = {
            "segments":  ["all", "clickers", "waitlisters", "buyers"],
            "buyers_pending": buyers_placeholder,
            "all":         persona_for(cur, "_aud_all"),
            "clickers":    persona_for(cur, "_aud_clickers"),
            "waitlisters": persona_for(cur, "_aud_waitlisters"),
            "buyers":      persona_for(cur, "_aud_buyers"),
        }
        M["persona"] = persona

        # ── acquisition: UTM breakdown PER ENTITY ──
        # Waitlist and buyers are DIFFERENT products, so their acquisition is kept
        # separate and the dashboard lets you pick which entity to view. The shape
        # is identical for every entity, so when buyer UTMs are added later they
        # appear automatically (utm_entity auto-detects the columns). clickers/all
        # are placeholders today (future: parse UTMs from the click URL / union).
        # Acquisition is meaningful only for the two sign-up entities — waitlist
        # (utm_* on the CM table) and buyers (members.oid -> landing.oid utm_*).
        # "all" merges the two. Clickers are intentionally excluded here.
        acq_waitlist = waitlist_utm(cur, f"{SA_SCHEMA}.awe_waitlist", available=waitlist_exists)
        acq_buyers   = buyers_utm(cur, members_tbl, AWE_LANDING_TABLE, available=not buyers_placeholder)
        M["acquisition"] = {
            "entities": ["all", "waitlist", "buyers"],
            "all":      merge_utm([acq_waitlist, acq_buyers]),
            "waitlist": acq_waitlist,
            "buyers":   acq_buyers,
        }

        # ── quiz uptake (derived from persona matched counts) ──
        def uptake(seg):
            t = persona[seg]["audience_total"]
            k = persona[seg]["matched_quiz"]
            return {"total": t, "took_quiz": k, "pct": round(100.0 * k / t, 1) if t else None}
        M["quiz_uptake"] = {
            "all":         uptake("all"),
            "clickers":    uptake("clickers"),
            "waitlisters": uptake("waitlisters"),
            "buyers":      uptake("buyers"),
        }

        # ── checkout landing events: static historical (GA) + full table, by acq bucket ──
        # DEDUPED BY VISITOR (oid): the "All" total = distinct visitors across every
        # bucket; each bucket/brand section = distinct visitors within it (a person
        # who landed via two brands counts in each, like unique clickers). Static GA
        # counts have no per-visitor identity, so they are added as-is. Landing rows
        # with no oid are each counted as their own visitor (can't be deduped).
        bucket_static = defaultdict(int)          # bucket -> static count
        day_static    = defaultdict(int)          # day    -> static count
        bday_static   = defaultdict(int)          # (brand, day) -> static count
        for bkt, day, cnt in AWE_CHECKOUT_STATIC:
            bucket_static[bkt] += cnt
            day_static[day]    += cnt
            bday_static[(bucket_brand(bkt), day)] += cnt
        # Checkout is shown as TOTAL EVENTS (non-unique, additive) with distinct-oid
        # as a secondary number. `*_ev` count every checkout row (n); `*_oids` track
        # distinct oid for the secondary "N distinct" line.
        bucket_ev = defaultdict(int); bucket_oids = defaultdict(set)   # events + distinct, per bucket
        brand_ev  = defaultdict(int); brand_oids  = defaultdict(set)   # per brand
        day_ev    = defaultdict(int)                                   # events per day
        bday_ev   = defaultdict(int)                                   # events per (brand, day)
        all_oids  = set()
        # Google Ads main-page landings (NOT checkout) — non-unique event counts,
        # top-of-funnel. Kept separate from checkout entirely.
        gads_brand     = defaultdict(int)   # brand -> landed-via-google-ads events
        gads_day_brand = defaultdict(lambda: defaultdict(int))  # brand -> {day: n}
        landing_available = table_exists(cur, AWE_LANDING_TABLE)
        if landing_available:
            lcols = columns_of(cur, AWE_LANDING_TABLE)
            datecol = find_col(lcols, _LANDING_TS_CANDIDATES)
            sel_day = f'"{datecol}"::date' if datecol else "NULL::date"
            has_oid   = "oid" in lcols
            has_camp  = "utm_campaign" in lcols
            has_oe    = "o_event" in lcols
            has_purl  = "product_url" in lcols
            sel_oid  = "NULLIF(LOWER(TRIM(oid::text)),'')" if has_oid else "NULL::text"
            sel_camp = "LOWER(TRIM(utm_campaign))" if has_camp else "NULL::text"
            sel_oe   = "LOWER(TRIM(o_event))"      if has_oe   else "NULL::text"
            sel_purl = "LOWER(TRIM(product_url))"  if has_purl else "NULL::text"
            if "utm_source" in lcols and "utm_medium" in lcols:
                cur.execute(f"""
                    SELECT {sel_oid} AS oid, LOWER(TRIM(utm_source)) AS src,
                           LOWER(TRIM(utm_medium)) AS med, {sel_camp} AS camp,
                           {sel_oe} AS oevent, {sel_purl} AS purl, {sel_day} AS day, COUNT(*) AS n
                    FROM {AWE_LANDING_TABLE} GROUP BY 1, 2, 3, 4, 5, 6, 7
                """)
                rows = cur.fetchall()
            else:
                cur.execute(f"SELECT {sel_oid} AS oid, {sel_camp} AS camp, {sel_oe} AS oevent, "
                            f"{sel_purl} AS purl, {sel_day} AS day, COUNT(*) AS n "
                            f"FROM {AWE_LANDING_TABLE} GROUP BY 1, 2, 3, 4, 5")
                rows = [dict(r, src=None, med=None) for r in cur.fetchall()]
            for r in rows:
                day    = str(r["day"]) if r["day"] else None
                camp   = r.get("camp")
                purl   = r.get("purl") or ""
                n      = safe_int(r["n"])
                # Discriminate by product_url (never null): the checkout URL contains
                # 'checkout', the course main page contains 'awecourse'. If the table
                # has no product_url column, fall back to o_event for checkout.
                if has_purl:
                    is_checkout = AWE_CHECKOUT_URL_MATCH in purl
                    is_landing  = (not is_checkout) and (AWE_LANDING_URL_MATCH in purl)
                else:
                    is_checkout = (r.get("oevent") == AWE_CHECKOUT_OEVENT)
                    is_landing  = (not is_checkout) and (camp == AWE_GOOGLE_ADS_CAMPAIGN)
                if not is_checkout:
                    # Course main-page landing (top of funnel, "Landed via Google Ads").
                    # Counted regardless of utm_campaign (may be null in future), and
                    # never mixed into checkout. Anything that is neither is ignored.
                    if is_landing:
                        gbrand = _BRAND_LABEL.get(r.get("src"), "SuperAge")
                        gads_brand[gbrand] += n
                        if day: gads_day_brand[gbrand][day] += n
                    continue
                # Checkout event -> a bucket. Google Ads checkout redirects get their
                # own bucket (never SuperAge Website / Organic).
                if camp == AWE_GOOGLE_ADS_CAMPAIGN:
                    bkt = AWE_GOOGLE_ADS_BUCKET
                else:
                    bkt = classify_acq(r["src"], r["med"], null_src="superage", null_med="website")
                brand = bucket_brand(bkt)
                oid   = r["oid"]
                bucket_ev[bkt] += n; brand_ev[brand] += n
                if day: day_ev[day] += n; bday_ev[(brand, day)] += n
                if oid:
                    bucket_oids[bkt].add(oid); brand_oids[brand].add(oid); all_oids.add(oid)
        # PRIMARY = total events (tracked rows + static, fully additive). Distinct oid
        # is a SECONDARY number shown beside it (not additive across buckets/brands).
        all_buckets = set(bucket_static) | set(bucket_ev)
        bucket_counts = {b: bucket_ev[b] + bucket_static[b] for b in all_buckets}   # events
        all_days = set(day_static) | set(day_ev)
        cday = {d: day_ev[d] + day_static[d] for d in all_days}                     # events
        co_brand = defaultdict(int)               # per-brand checkout EVENTS (drives by_brand)
        codist_brand = defaultdict(int)           # per-brand distinct oid (secondary)
        for b in set(brand_ev) | {bucket_brand(x) for x in bucket_static}:
            static_b = sum(v for k, v in bucket_static.items() if bucket_brand(k) == b)
            co_brand[b]     = brand_ev[b] + static_b
            codist_brand[b] = len(brand_oids[b])
        # per-brand daily checkout series (events; drives the brand-filtered Checkout Trend)
        checkout_day_by_brand = defaultdict(list)
        _bd_keys = set(bday_static) | set(bday_ev)
        _bd = defaultdict(dict)
        for (b, d) in _bd_keys:
            _bd[b][d] = bday_ev[(b, d)] + bday_static[(b, d)]
        for b in _bd:
            checkout_day_by_brand[b] = [{"day": d, "count": _bd[b][d]} for d in sorted(_bd[b])]
        checkout_by_bucket = sorted(
            [{"bucket": b, "brand": bucket_brand(b), "count": c} for b, c in bucket_counts.items()],
            key=lambda x: x["count"], reverse=True)
        checkout_by_day = [{"day": d, "count": cday[d]} for d in sorted(cday)]
        # All: total events = every tracked checkout row + all static; distinct = distinct oid.
        checkout_total    = sum(bucket_ev.values()) + sum(bucket_static.values())
        checkout_distinct = len(all_oids)
        # Organic / direct checkout events (the "... Website / Organic" bucket) — these
        # did NOT come from campaigns or ads, so the funnel annotates them separately.
        organic_brand = defaultdict(int)
        for row in checkout_by_bucket:
            if "organic" in row["bucket"].lower():
                organic_brand[row["brand"]] += row["count"]
        organic_all = sum(organic_brand.values())
        M["checkout"] = {"total": checkout_total, "distinct": checkout_distinct,
                         "organic": organic_all,
                         "by_bucket": checkout_by_bucket, "by_day": checkout_by_day}

        # Google Ads main-page landings (non-unique) — per brand + per day + All.
        gads_total = sum(gads_brand.values())
        gads_day_all = defaultdict(int)
        for b in gads_day_brand:
            for d, n in gads_day_brand[b].items():
                gads_day_all[d] += n
        gads_by_day_all   = [{"day": d, "count": gads_day_all[d]} for d in sorted(gads_day_all)]
        gads_by_day_brand = {b: [{"day": d, "count": gads_day_brand[b][d]} for d in sorted(gads_day_brand[b])]
                             for b in gads_day_brand}

        # converted = members with a landing click at/before purchase (attribution)
        converted_buyers   = safe_int(acq_buyers.get("attributed", 0))
        wl_total           = wl["total"]
        wl_also_bought     = safe_int(buyers.get("waitlist_buyers", 0))
        wl_also_bought_pct = round(100.0 * wl_also_bought / wl_total, 1) if wl_total else None

        # ── per-brand overview (drives the page-level brand filter) ──
        brand_clicks = {b["brand"]: b for b in clicks_by_brand}
        wl_brand = defaultdict(int)
        if waitlist_exists:
            cur.execute(f"""
                SELECT COALESCE(NULLIF(LOWER(TRIM(utm_source)),''),'superage') AS src, COUNT(*) AS n
                FROM {SA_SCHEMA}.awe_waitlist WHERE {WL_ACTIVE} GROUP BY 1
            """)
            for r in cur.fetchall():
                wl_brand[_BRAND_LABEL.get(r["src"], "Other")] += safe_int(r["n"])
        # co_brand (per-brand distinct checkout visitors) computed with the checkout
        # section above.
        mem_brand, conv_brand = defaultdict(int), defaultdict(int)
        if table_exists(cur, AWE_PURCHASER_MATVIEW):
            cur.execute(f"""
                SELECT COALESCE(NULLIF(LOWER(TRIM(utm_source)),''),'superage') AS src,
                       COUNT(*) AS total, COUNT(*) FILTER (WHERE attributed) AS conv
                FROM {AWE_PURCHASER_MATVIEW} GROUP BY 1
            """)
            for r in cur.fetchall():
                bl = _BRAND_LABEL.get(r["src"], "Other")
                mem_brand[bl] += safe_int(r["total"])
                conv_brand[bl] += safe_int(r["conv"])

        # per-brand time series + crossover for the brand-filtered Community Members
        # + Trends sections. Member brand attribution uses the purchaser matview
        # (utm_source -> brand, null => superage), matching mem_brand above.
        mem_growth_brand = defaultdict(list)   # brand -> [{day,count}] members by join date
        wl_growth_brand  = defaultdict(list)   # brand -> [{day,count}] waitlist by subscribe date
        wlmem_brand      = defaultdict(int)    # brand -> members who were on the active waitlist
        _has_pma = table_exists(cur, AWE_PURCHASER_MATVIEW)
        _brand_src = (f"(SELECT email, COALESCE(NULLIF(LOWER(TRIM(utm_source)),''),'superage') AS src "
                      f"FROM {AWE_PURCHASER_MATVIEW})")
        if members_exists and _has_pma:
            cur.execute(f"""
                SELECT p.src AS src, m.circle_created_at::date AS day,
                       COUNT(DISTINCT LOWER(TRIM(m.email))) AS n
                FROM {members_tbl} m
                JOIN {_brand_src} p ON LOWER(TRIM(m.email)) = p.email
                WHERE m.circle_created_at IS NOT NULL AND {NONBLANK.replace('email','m.email')}
                GROUP BY 1, 2 ORDER BY 2
            """)
            for r in cur.fetchall():
                mem_growth_brand[_BRAND_LABEL.get(r["src"], "Other")].append(
                    {"day": str(r["day"]), "count": safe_int(r["n"])})
            if table_exists(cur, f"{SA_SCHEMA}.awe_waitlist"):
                cur.execute(f"""
                    SELECT p.src AS src, COUNT(DISTINCT LOWER(TRIM(m.email))) AS n
                    FROM {members_tbl} m
                    JOIN {SA_SCHEMA}.awe_waitlist w ON LOWER(TRIM(m.email)) = LOWER(TRIM(w.email))
                    JOIN {_brand_src} p ON LOWER(TRIM(m.email)) = p.email
                    WHERE m.email IS NOT NULL AND TRIM(m.email) != ''{SUP_M} AND w.{WL_ACTIVE}
                    GROUP BY 1
                """)
                for r in cur.fetchall():
                    wlmem_brand[_BRAND_LABEL.get(r["src"], "Other")] += safe_int(r["n"])
        if waitlist_exists:
            cur.execute(f"""
                SELECT COALESCE(NULLIF(LOWER(TRIM(utm_source)),''),'superage') AS src,
                       date_subscribed::date AS day, COUNT(*) AS n
                FROM {SA_SCHEMA}.awe_waitlist
                WHERE {WL_ACTIVE} AND date_subscribed IS NOT NULL
                GROUP BY 1, 2 ORDER BY 2
            """)
            for r in cur.fetchall():
                wl_growth_brand[_BRAND_LABEL.get(r["src"], "Other")].append(
                    {"day": str(r["day"]), "count": safe_int(r["n"])})

        def _funnel(uniq, total, wlc, coc, codist, coorg, convc, memc, gads):
            # `uniq` = unique clickers (the % base, per spec); `total` = total
            # (non-unique) clicks shown as the top box's big number; `coc` = total
            # checkout EVENTS (big), `codist` = distinct oid (small line), `coorg` =
            # organic/direct checkout events (annotated separately — not from
            # campaigns/ads); `gads` = Google Ads main-page landings (2nd top box).
            return {
                "top": {"label": "Clicks", "count": total, "unique": uniq},
                "google_ads": {"label": "Google Ads", "count": gads},
                "waitlist": {"label": "Waitlist · Nervous System course", "count": wlc,
                             "pct_of_top": round(100.0 * wlc / uniq, 1) if uniq else None},
                "landing": {"available": True, "label": "Checkout Events", "count": coc,
                            "distinct": codist, "organic": coorg,
                            "pct_of_top": round(100.0 * coc / uniq, 1) if uniq else None},
                # The final node shows ALL members (memc), not just the ones a
                # checkout click captured — checkout tracking started late, so the
                # untracked members are attributed by their acquisition (null =>
                # SuperAge). `attributed` keeps the checkout-matched subset (convc)
                # for context.
                "buyers": {"label": "Community Members", "count": memc,
                           "attributed": convc,
                           "pct_of_landing": round(100.0 * memc / coc, 1) if coc else None,
                           "total_buyers": memc},
                "waitlist_also_bought": {"count": wl_also_bought, "pct": wl_also_bought_pct},
            }

        by_brand = {}
        for bl in AWE_BRANDS:
            bc = brand_clicks.get(bl, {})
            top = safe_int(bc.get("unique_clickers", 0))
            tot = safe_int(bc.get("clicks", 0))
            by_brand[bl] = {
                "unique_clickers":  top,
                "total_clicks":     tot,
                "waitlist_total":   wl_brand.get(bl, 0),
                "checkout_total":   co_brand.get(bl, 0),
                "checkout_distinct": codist_brand.get(bl, 0),
                "converted_buyers": conv_brand.get(bl, 0),
                "members_total":    mem_brand.get(bl, 0),
                "members_from_waitlist": wlmem_brand.get(bl, 0),
                "google_ads_landings":   gads_brand.get(bl, 0),
                "clicks_by_day":    clicks_day_by_brand.get(bl, []),
                "checkout_by_day":  checkout_day_by_brand.get(bl, []),
                "google_ads_by_day": gads_by_day_brand.get(bl, []),
                "members_growth":   mem_growth_brand.get(bl, []),
                "waitlist_growth":  wl_growth_brand.get(bl, []),
                "funnel": _funnel(top, tot, wl_brand.get(bl, 0), co_brand.get(bl, 0),
                                  codist_brand.get(bl, 0), organic_brand.get(bl, 0),
                                  conv_brand.get(bl, 0), mem_brand.get(bl, 0), gads_brand.get(bl, 0)),
            }
        by_brand["All"] = {
            "unique_clickers":  distinct_clickers,
            "total_clicks":     total_clicks,
            "waitlist_total":   wl_total,
            "checkout_total":   checkout_total,
            "checkout_distinct": checkout_distinct,
            "converted_buyers": converted_buyers,
            "members_total":    buyers_total,
            "members_from_waitlist": wl_also_bought,
            "google_ads_landings":   gads_total,
            "clicks_by_day":    clicks_by_day,
            "checkout_by_day":  checkout_by_day,
            "google_ads_by_day": gads_by_day_all,
            "members_growth":   buyers["growth"],
            "waitlist_growth":  wl["growth"],
            "funnel": _funnel(distinct_clickers, total_clicks, wl_total, checkout_total,
                              checkout_distinct, organic_all, converted_buyers, buyers_total, gads_total),
        }
        M["by_brand"] = by_brand
        M["brands"]   = ["All"] + AWE_BRANDS

        # ── KPIs + funnel (All view; the frontend swaps per selected brand) ──
        click_to_waitlist = round(100.0 * wl_total / distinct_clickers, 1) if distinct_clickers else None
        click_to_checkout = round(100.0 * checkout_total / distinct_clickers, 1) if distinct_clickers else None
        checkout_to_member = round(100.0 * converted_buyers / checkout_total, 1) if checkout_total else None
        M["kpis"] = {
            "distinct_clickers":     distinct_clickers,
            "total_clicks":          total_clicks,
            "waitlist_total":        wl_total,
            "landing_events":        checkout_total,
            "checkout_distinct":     checkout_distinct,
            "google_ads_landings":   gads_total,
            "converted_buyers":      converted_buyers,
            "landing_to_buyer_pct":  checkout_to_member,
            "click_to_checkout_pct": click_to_checkout,
            "buyers_total":          buyers_total,
            "revenue_usd":           buyers["estimated_revenue_usd"],
            "click_to_waitlist_pct": click_to_waitlist,
            "quiz_takers_audience":  persona["all"]["matched_quiz"],
        }
        M["funnel"] = by_brand["All"]["funnel"]

    finally:
        cur.close()
        conn.close()

    # ── metadata ──
    try:
        from zoneinfo import ZoneInfo
        now_est = datetime.now(ZoneInfo("America/New_York"))
        try:
            M["last_updated"] = now_est.strftime("%b %-d, %Y %I:%M %p EST")
        except ValueError:  # Windows lacks %-d
            M["last_updated"] = now_est.strftime("%b %#d, %Y %I:%M %p EST")
        M["generated_at"] = now_est.isoformat()
    except Exception:
        M["last_updated"] = datetime.utcnow().strftime("%b %d, %Y %I:%M %p UTC")
        M["generated_at"] = datetime.utcnow().isoformat()
    M["awe_url_patterns"] = AWE_URL_PATTERNS

    content = json.dumps(M, indent=2, default=str)
    result = write_to_r2(content)
    logger.info("AWE metrics done -- clickers=%s waitlist=%s clicks=%s upload=%s",
                M["kpis"]["distinct_clickers"], M["kpis"]["waitlist_total"],
                M["kpis"]["total_clicks"], result.get("uploaded"))

    return {"statusCode": 200, "body": content, "upload": result}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    os.environ.setdefault("WRITE_TO_R2", "false")
    print(lambda_handler({}, None)["upload"])
