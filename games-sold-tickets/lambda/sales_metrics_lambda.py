

"""
Sales Metrics Lambda — Python 3.12
Queries RDS (public.games_tickets, public.waitlist_emails,
superage.games_landing_events, superage."Campaigns_Clicks",
ageist.ageist_clicks, ageist.ageist_campaigns,
public.allhealthy_contact_clicks via separate DB),
computes metrics, writes games-sold-tickets/sales_metrics.json to Cloudflare R2.

Landing-event sourcing model
─────────────────────────────
Only rows from games_landing_events where utm_source is in a known list
(our brands + sponsors + events) are counted. Unknown/null sources are excluded.

superage / allhealthy / ageist email clicks come from raw click tables only —
their email rows in games_landing_events are excluded to avoid double-counting.

superage WEBSITE rows in games_landing_events ARE included and are combined
with SA raw email clicks to form the total superage bucket.

Source lists:
  OUR_BRAND_SOURCES = superage, allhealthy, ageist, fitnessquiz,
                      optimism, optimism_team, david_stewart
  SPONSOR_SOURCES   = whoop, altra, pur, braun, buck_institute, junior, pvolve
  EVENT_SOURCES     = cal_tri, global_wellness, the_pump, lifetime, adventure_women
  KNOWN_SOURCES     = OUR_BRAND_SOURCES + SPONSOR_SOURCES + EVENT_SOURCES

Raw email click tables (replace games_landing_events email rows):
  SA  -> superage."Campaigns_Clicks"          (URL filter, date "Date")
  AH  -> public.allhealthy_contact_clicks     (data::text filter, date event_timestamp)
  AG  -> ageist.ageist_clicks                 (final_url filter, date campaign_send_time)

Required env vars:
  DB_SECRET_ARN   -- Secrets Manager ARN (covers superage.*, ageist.*, public.*)
  R2_SECRET_ARN   -- Secrets Manager ARN; secret must carry keys:
                     account_id, access_key_id, secret_access_key, bucket_name
  AH_DB_HOST      -- AllHealthy DB host
  AH_DB_NAME      -- AllHealthy DB name
  AH_DB_USER      -- AllHealthy DB user
  AH_DB_PASSWORD  -- AllHealthy DB password

Optional env vars:
  DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_SSLMODE
  AH_DB_PORT (default 5432) / AH_DB_SSLMODE (default require)
  R2_FILE_PATH      (default: games-sold-tickets/sales_metrics.json)
  WRITE_TO_R2       (default: true; set false for local/test runs)
  GAME_URL_PATTERNS (default: %o.superage.com/r?dest=games.superage.com%,%games.superage.com%)
"""

import json
import os
import logging
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_db_secret_cache = None
_r2_secret_cache = None
_r2_client_cache = None

R2_FILE_PATH = os.environ.get("R2_FILE_PATH", "games-sold-tickets/sales_metrics.json")
WRITE_TO_R2  = os.environ.get("WRITE_TO_R2", "true").strip().lower() not in {"0", "false", "no"}

# Games URL patterns
GAME_URL_PATTERNS = [
    x.strip()
    for x in os.environ.get(
        "GAME_URL_PATTERNS",
        "%o.superage.com/r?dest=games.superage.com%,%games.superage.com%",
    ).split(",")
    if x.strip()
]

# Source classification
# Derived from utm_source values observed in games_landing_events.
# Only rows with utm_source IN KNOWN_SOURCES are counted in any landing metric.
OUR_BRAND_SOURCES = [
    "superage",
    "allhealthy",
    "ageist",
    "healthbrief",
    "fitnessquiz",
    "optimism",
    "optimism_team",
    "david_stewart",
]
SPONSOR_SOURCES = [
    "whoop",
    "altra",
    "pur",
    "braun",
    "buck_institute",
    "junior",
    "pvolve",
]
EVENT_SOURCES = [
    "cal_tri",
    "global_wellness",
    "the_pump",
    "lifetime",
    "adventure_women",
]
KNOWN_SOURCES = OUR_BRAND_SOURCES + SPONSOR_SOURCES + EVENT_SOURCES

# These three brand sources have their EMAIL clicks sourced from raw click tables.
# Their email rows in games_landing_events are excluded to avoid double-counting.
# superage WEBSITE rows are kept and combined with SA raw email clicks.
RAW_EMAIL_SOURCES = ["superage", "allhealthy", "ageist", "healthbrief"]


# ─────────────────────────────────────────────────────────────
# R2 helpers
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
        logger.warning("WRITE_TO_R2=false -- skipping upload.")
        return {"uploaded": False, "reason": "dry_run"}
    try:
        client, bucket = _get_r2_client()
        client.put_object(
            Bucket=bucket,
            Key=R2_FILE_PATH,
            Body=content.encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("R2 upload OK -- bucket=%s key=%s", bucket, R2_FILE_PATH)
        return {"uploaded": True, "bucket": bucket, "key": R2_FILE_PATH}
    except Exception as e:
        logger.error("R2 upload failed: %s", e)
        return {"uploaded": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# DB connections
# ─────────────────────────────────────────────────────────────

def _get_db_secret():
    global _db_secret_cache
    if _db_secret_cache is not None:
        return _db_secret_cache
    client = boto3.client(
        "secretsmanager",
        region_name=os.environ.get("AWS_REGION", "us-west-1"),
    )
    secret = json.loads(
        client.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"]
    )
    _db_secret_cache = secret
    logger.info("DB secret fetched from Secrets Manager.")
    return secret


def get_connection():
    """Main DB -- covers public.*, superage.*, ageist.* schemas."""
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


def ah_connection():
    """AllHealthy DB -- credentials from AH_DB_* env vars."""
    return psycopg2.connect(
        host     = os.environ["AH_DB_HOST"],
        port     = int(os.environ.get("AH_DB_PORT", 5432)),
        dbname   = os.environ["AH_DB_NAME"],
        user     = os.environ["AH_DB_USER"],
        password = os.environ["AH_DB_PASSWORD"],
        sslmode  = os.environ.get("AH_DB_SSLMODE", "require"),
        connect_timeout=30,
    )


# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────

def cols_of(cur, schema, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    )
    result = [r["column_name"] for r in cur.fetchall()]
    logger.info("Columns %s.%s -> %s", schema, table, result)
    return result


def find_col(cols, candidates):
    return next((c for c in candidates if c in cols), None)


def safe_int(v, default=0):
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    logger.info("Sales metrics Lambda starting -- r2_key=%s", R2_FILE_PATH)

    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:

        # ════════════════════════════════════════════════════
        # COLUMN DISCOVERY
        # ════════════════════════════════════════════════════

        ticket_cols = cols_of(cur, "public", "games_tickets")
        t_email  = find_col(ticket_cols, ["email", "customer_email", "buyer_email"])
        t_date   = find_col(ticket_cols, ["created_at", "purchased_at", "purchase_date",
                                           "ticket_date", "timestamp"])
        t_type   = find_col(ticket_cols, ["ticket_type", "type", "tier", "category",
                                           "ticket_tier"])
        t_dob    = find_col(ticket_cols, ["date_of_birth", "dob", "birth_date", "birthdate"])
        t_gender = find_col(ticket_cols, ["gender", "sex"])
        t_city   = find_col(ticket_cols, ["city", "town", "location"])
        t_oid    = find_col(ticket_cols, ["oid", "order_id", "id", "ticket_id",
                                           "booking_id", "reference"])
        logger.info(
            "t_email=%s t_date=%s t_type=%s t_dob=%s t_gender=%s t_city=%s",
            t_email, t_date, t_type, t_dob, t_gender, t_city,
        )

        WAITLIST_FILTER = (
            "email_oversight_result NOT IN ('Bot','Undeliverable','Malformed','SpamTrap') "
            "AND is_suppressed = false"
        )

        # ════════════════════════════════════════════════════
        # TICKET TOTALS
        # ════════════════════════════════════════════════════

        cur.execute("SELECT COUNT(*) AS n FROM public.games_tickets")
        total_tickets = safe_int(cur.fetchone()["n"])

        cur.execute(
            f"SELECT COUNT(DISTINCT email) AS n "
            f"FROM public.waitlist_emails WHERE {WAITLIST_FILTER}"
        )
        total_waitlist = safe_int(cur.fetchone()["n"])

        # Waitlist crossover
        waitlist_buyers = None
        if t_email:
            cur.execute(f"""
                SELECT COUNT(*) AS n FROM public.games_tickets t
                WHERE {t_email} IN (
                    SELECT DISTINCT ON (email) email
                    FROM public.waitlist_emails
                    WHERE {WAITLIST_FILTER}
                    ORDER BY email, created_at ASC
                )
            """)
            waitlist_buyers = safe_int(cur.fetchone()["n"])

        # Ticket types with waitlist overlap
        ticket_types = []
        if t_type:
            if t_email:
                cur.execute(f"""
                    SELECT COALESCE({t_type},'Unknown') AS type,
                           COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE {t_email} IN (
                               SELECT DISTINCT ON (email) email
                               FROM public.waitlist_emails
                               WHERE {WAITLIST_FILTER}
                               ORDER BY email, created_at ASC
                           )) AS on_waitlist
                    FROM public.games_tickets GROUP BY 1 ORDER BY 2 DESC
                """)
            else:
                cur.execute(f"""
                    SELECT COALESCE({t_type},'Unknown') AS type,
                           COUNT(*) AS total, 0 AS on_waitlist
                    FROM public.games_tickets GROUP BY 1 ORDER BY 2 DESC
                """)
            for r in cur.fetchall():
                tot = safe_int(r["total"])
                wl  = safe_int(r["on_waitlist"])
                ticket_types.append({
                    "type":        r["type"],
                    "total":       tot,
                    "on_waitlist": wl,
                    "direct":      tot - wl,
                })

        # Age distribution
        age_distribution = []
        if t_dob:
            cur.execute(f"""
                SELECT
                  CASE
                    WHEN DATE_PART('year', AGE({t_dob})) < 35 THEN 'Under 35'
                    WHEN DATE_PART('year', AGE({t_dob})) < 45 THEN '35-44'
                    WHEN DATE_PART('year', AGE({t_dob})) < 55 THEN '45-54'
                    WHEN DATE_PART('year', AGE({t_dob})) < 65 THEN '55-64'
                    WHEN DATE_PART('year', AGE({t_dob})) < 75 THEN '65-74'
                    ELSE '75+'
                  END AS range,
                  COUNT(*) AS count
                FROM public.games_tickets
                WHERE {t_dob} IS NOT NULL
                GROUP BY 1
                ORDER BY MIN(DATE_PART('year', AGE({t_dob})))
            """)
            age_distribution = [
                {"range": r["range"], "count": safe_int(r["count"])}
                for r in cur.fetchall()
            ]

        # Gender distribution
        gender_distribution = []
        if t_gender:
            cur.execute(f"""
                SELECT COALESCE(INITCAP({t_gender}::text), 'Unknown') AS gender,
                       COUNT(*) AS count
                FROM public.games_tickets
                GROUP BY 1 ORDER BY 2 DESC
            """)
            gender_distribution = [
                {"gender": r["gender"], "count": safe_int(r["count"])}
                for r in cur.fetchall()
            ]

        # City distribution (top 10)
        city_distribution = []
        if t_city:
            cur.execute(f"""
                SELECT COALESCE({t_city}, 'Unknown') AS city, COUNT(*) AS count
                FROM public.games_tickets
                WHERE {t_city} IS NOT NULL AND TRIM({t_city}) != ''
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10
            """)
            city_distribution = [
                {"city": r["city"], "count": safe_int(r["count"])}
                for r in cur.fetchall()
            ]

        # Estimated revenue
        TICKET_PRICES = {"champion pass": 1299, "athlete pass": 399, "spectator": 30}
        estimated_revenue = None
        if t_type:
            cur.execute(
                f"SELECT LOWER(COALESCE({t_type},'')) AS type, COUNT(*) AS n "
                f"FROM public.games_tickets GROUP BY 1"
            )
            estimated_revenue = sum(
                safe_int(r["n"]) * TICKET_PRICES[r["type"]]
                for r in cur.fetchall()
                if r["type"] in TICKET_PRICES
            )

        # Recent tickets
        order_clause = f"ORDER BY {t_date} DESC" if t_date else ""
        cur.execute(f"SELECT * FROM public.games_tickets {order_clause} LIMIT 20")
        recent_rows = [dict(r) for r in cur.fetchall()]

        # ════════════════════════════════════════════════════
        # PERSONA — Subscriber Quiz Join
        # Joins unique buyer emails against superage.subscriber_quiz,
        # taking the latest quiz entry per email (DISTINCT ON).
        # ════════════════════════════════════════════════════

        persona: dict = {
            "total_tickets":       total_tickets,
            "unique_buyer_emails": 0,
            "matched_buyers":      0,
            "match_rate_pct":      None,
            "avg_longevity_score": None,
            "avg_age":             None,
            "gender":              [],
            "longevity_buckets":   [],
            "financial_situation": [],
            "education_level":     [],
            "marital_status":      [],
            "sleep_hours":         [],
            "exercise_freq":       [],
            "smoking_status":      [],
            "is_obese":            [],
            "alcohol_freq":        [],
            "stress_impact":       [],
        }

        if t_oid:
            # Join on oid — more reliable than email (stable system ID, survives email changes).
            # oid join yields more matches than email join when buyers changed their email.
            _PERSONA_CTE = f"""
                buyer_oids AS (
                  SELECT DISTINCT LOWER(TRIM({t_oid})) AS oid
                  FROM public.games_tickets
                  WHERE {t_oid} IS NOT NULL AND TRIM({t_oid}) != ''
                ),
                quiz_deduped AS (
                  SELECT DISTINCT ON (LOWER(TRIM(sq.oid)))
                    LOWER(TRIM(sq.oid))     AS oid,
                    sq.longevity_score, sq.age, sq.gender,
                    sq.financial_situation, sq.education_level, sq.marital_status,
                    sq.sleep_hours, sq.smoking_status, sq.is_obese,
                    sq.alcohol_freq, sq.stress_impact,
                    COALESCE(sq.exercise_freq, sq.exercise_freq_male,
                             sq.exercise_freq_female, sq.exercise_freq_other) AS exercise_freq
                  FROM superage.subscriber_quiz sq
                  WHERE sq.oid IS NOT NULL AND TRIM(sq.oid) != ''
                  ORDER BY LOWER(TRIM(sq.oid)), sq.created_at DESC
                )
            """

            # Unique buyer oid count
            cur.execute(f"""
                SELECT COUNT(DISTINCT LOWER(TRIM({t_oid}))) AS n
                FROM public.games_tickets
                WHERE {t_oid} IS NOT NULL AND TRIM({t_oid}) != ''
            """)
            unique_buyer_emails = safe_int(cur.fetchone()["n"])
            persona["unique_buyer_emails"] = unique_buyer_emails

            # Match count + averages
            cur.execute(f"""
                WITH {_PERSONA_CTE}
                SELECT COUNT(*) AS matched,
                       ROUND(AVG(qd.longevity_score)::numeric, 2) AS avg_longevity,
                       ROUND(AVG(qd.age)::numeric, 1)             AS avg_age
                FROM buyer_oids be
                INNER JOIN quiz_deduped qd ON be.oid = qd.oid
            """)
            r = cur.fetchone()
            persona["matched_buyers"]      = safe_int(r["matched"])
            persona["avg_longevity_score"] = float(r["avg_longevity"]) if r["avg_longevity"] is not None else None
            persona["avg_age"]             = float(r["avg_age"])       if r["avg_age"]       is not None else None
            if unique_buyer_emails > 0:
                persona["match_rate_pct"] = round(
                    100.0 * persona["matched_buyers"] / unique_buyer_emails, 1
                )

            # Longevity score buckets
            cur.execute(f"""
                WITH {_PERSONA_CTE}
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
                ORDER BY MIN(qd.longevity_score)
            """)
            persona["longevity_buckets"] = [
                {"bucket": r["bucket"], "count": safe_int(r["count"])}
                for r in cur.fetchall()
            ]

            def _persona_dist(col: str, key: str) -> list:
                cur.execute(f"""
                    WITH {_PERSONA_CTE}
                    SELECT
                        CASE
                            WHEN qd.{col} IS NULL OR TRIM(qd.{col}::text) = ''
                            THEN 'Not specified'
                            ELSE qd.{col}::text
                        END AS val,
                        COUNT(*) AS count
                    FROM buyer_oids be
                    INNER JOIN quiz_deduped qd ON be.oid = qd.oid
                    GROUP BY 1 ORDER BY 2 DESC
                """)
                return [{key: r["val"], "count": safe_int(r["count"])} for r in cur.fetchall()]

            persona["gender"]              = _persona_dist("gender",             "gender")
            persona["financial_situation"] = _persona_dist("financial_situation","financial_situation")
            persona["education_level"]     = _persona_dist("education_level",    "education_level")
            persona["marital_status"]      = _persona_dist("marital_status",     "marital_status")
            persona["sleep_hours"]         = _persona_dist("sleep_hours",        "sleep_hours")
            persona["exercise_freq"]       = _persona_dist("exercise_freq",      "exercise_freq")
            persona["smoking_status"]      = _persona_dist("smoking_status",     "smoking_status")
            persona["is_obese"]            = _persona_dist("is_obese",           "is_obese")
            persona["alcohol_freq"]        = _persona_dist("alcohol_freq",       "alcohol_freq")
            persona["stress_impact"]       = _persona_dist("stress_impact",      "stress_impact")

            # All quiz takers avg longevity (for comparison)
            cur.execute("""
                SELECT ROUND(AVG(longevity_score)::numeric, 1) AS avg_ls
                FROM superage.subscriber_quiz
                WHERE longevity_score IS NOT NULL
            """)
            r = cur.fetchone()
            persona["all_quiz_avg_longevity"] = float(r["avg_ls"]) if r["avg_ls"] is not None else None

            # Per ticket type breakdown
            cur.execute(f"""
                WITH {_PERSONA_CTE}
                SELECT
                    COALESCE({t_type}, 'Unknown') AS ticket_type,
                    COUNT(*)                       AS count,
                    ROUND(AVG(qd.longevity_score)::numeric, 1) AS avg_longevity,
                    ROUND(AVG(qd.age)::numeric, 1)             AS avg_age
                FROM public.games_tickets gt
                INNER JOIN buyer_oids be ON LOWER(TRIM(gt.{t_oid})) = be.oid
                INNER JOIN quiz_deduped qd ON be.oid = qd.oid
                GROUP BY 1 ORDER BY 2 DESC
            """)
            persona["by_ticket_type"] = [
                {
                    "ticket_type":    r["ticket_type"],
                    "count":          safe_int(r["count"]),
                    "avg_longevity":  float(r["avg_longevity"]) if r["avg_longevity"] is not None else None,
                    "avg_age":        float(r["avg_age"])       if r["avg_age"]       is not None else None,
                }
                for r in cur.fetchall()
            ]

            # Unmatched buyers — gender + age from games_tickets
            _unmatched_where = f"""
                WHERE LOWER(TRIM({t_oid})) NOT IN (
                    SELECT DISTINCT LOWER(TRIM(oid)) FROM superage.subscriber_quiz
                    WHERE oid IS NOT NULL AND TRIM(oid) != ''
                )
            """
            if t_gender:
                cur.execute(f"""
                    SELECT COALESCE(INITCAP({t_gender}::text), 'Unknown') AS gender,
                           COUNT(DISTINCT LOWER(TRIM({t_oid}))) AS count
                    FROM public.games_tickets {_unmatched_where}
                    GROUP BY 1 ORDER BY 2 DESC
                """)
                persona["unmatched_gender"] = [
                    {"gender": r["gender"], "count": safe_int(r["count"])} for r in cur.fetchall()
                ]

            if t_dob:
                cur.execute(f"""
                    SELECT
                        CASE
                            WHEN DATE_PART('year', AGE({t_dob})) < 45 THEN 'Under 45'
                            WHEN DATE_PART('year', AGE({t_dob})) < 55 THEN '45-54'
                            WHEN DATE_PART('year', AGE({t_dob})) < 65 THEN '55-64'
                            WHEN DATE_PART('year', AGE({t_dob})) < 75 THEN '65-74'
                            ELSE '75+'
                        END AS range,
                        COUNT(DISTINCT LOWER(TRIM({t_oid}))) AS count
                    FROM public.games_tickets
                    WHERE {t_dob} IS NOT NULL
                      AND LOWER(TRIM({t_oid})) NOT IN (
                        SELECT DISTINCT LOWER(TRIM(oid)) FROM superage.subscriber_quiz
                        WHERE oid IS NOT NULL AND TRIM(oid) != ''
                      )
                    GROUP BY 1 ORDER BY MIN(DATE_PART('year', AGE({t_dob})))
                """)
                persona["unmatched_age"] = [
                    {"range": r["range"], "count": safe_int(r["count"])} for r in cur.fetchall()
                ]

        # ════════════════════════════════════════════════════
        # TICKET FUNNEL — Transaction Source Analysis
        # Joins games_tickets to superage.ticket_transactions on
        # transaction_id; uses session_* columns (not UTM in tickets).
        # ════════════════════════════════════════════════════

        ticket_funnel: dict = {
            "total_tickets":   total_tickets,
            "matched_tickets": 0,
            "by_type":         [],
        }

        cur.execute("""
            SELECT COUNT(*) AS matched
            FROM public.games_tickets gt
            INNER JOIN superage.ticket_transactions tt
              ON gt.transaction_id = tt.transaction_id
            WHERE gt.transaction_id IS NOT NULL
              AND TRIM(gt.transaction_id) != ''
        """)
        ticket_funnel["matched_tickets"] = safe_int(cur.fetchone()["matched"])

        cur.execute("""
            SELECT
              COALESCE(gt.ticket_type, 'Unknown') AS ticket_type,
              COALESCE(NULLIF(TRIM(tt.session_medium),   ''), '(none)')    AS session_medium,
              COUNT(*) AS count
            FROM public.games_tickets gt
            INNER JOIN superage.ticket_transactions tt
              ON gt.transaction_id = tt.transaction_id
            WHERE gt.transaction_id IS NOT NULL AND TRIM(gt.transaction_id) != ''
            GROUP BY 1, 2 ORDER BY 1, 3 DESC
        """)
        _medium_rows = cur.fetchall()

        cur.execute("""
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
            GROUP BY 1, 2 ORDER BY 1, 3 DESC
        """)
        _source_rows = cur.fetchall()

        cur.execute("""
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
            GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC
        """)
        _campaign_rows = cur.fetchall()

        _type_medium:   dict = defaultdict(list)
        _type_source:   dict = defaultdict(list)
        _type_campaign: dict = defaultdict(list)
        _seen_types:    list = []

        for r in _medium_rows:
            t = r["ticket_type"]
            if t not in _seen_types:
                _seen_types.append(t)
            _type_medium[t].append({"medium": r["session_medium"], "count": safe_int(r["count"])})

        for r in _source_rows:
            _type_source[r["ticket_type"]].append(
                {"source": r["session_source"], "count": safe_int(r["count"])}
            )

        for r in _campaign_rows:
            _type_campaign[r["ticket_type"]].append(
                {"campaign": r["session_campaign"], "source": r["session_source"], "count": safe_int(r["count"])}
            )

        ticket_funnel["by_type"] = [
            {
                "ticket_type": t,
                "by_medium":   _type_medium[t],
                "by_source":   _type_source[t],
                "by_campaign": [
                    c for c in _type_campaign[t]
                    if c["campaign"] != "(not set)"
                ],
            }
            for t in _seen_types
        ]

        # ════════════════════════════════════════════════════
        # FILTERED LANDING EVENTS
        #
        # Rule 1: only rows where utm_source IN KNOWN_SOURCES
        # Rule 2: exclude utm_source IN RAW_EMAIL_SOURCES AND utm_medium = 'email'
        #         (those are replaced by raw click table counts)
        # ════════════════════════════════════════════════════

        # Total filtered landing events
        cur.execute("""
            SELECT COUNT(*) AS n
            FROM superage.games_landing_events
            WHERE utm_source = ANY(%s)
              AND NOT (utm_source = ANY(%s) AND utm_medium = 'email')
        """, (KNOWN_SOURCES, RAW_EMAIL_SOURCES))
        filtered_landing_total = safe_int(cur.fetchone()["n"])

        # Per-source counts from filtered landing events.
        cur.execute("""
            SELECT utm_source AS source, COUNT(*) AS count
            FROM superage.games_landing_events
            WHERE utm_source = ANY(%s)
              AND NOT (utm_source = ANY(%s) AND utm_medium = 'email')
            GROUP BY utm_source
            ORDER BY count DESC
        """, (KNOWN_SOURCES, RAW_EMAIL_SOURCES))
        landing_source_from_events = {
            r["source"]: safe_int(r["count"])
            for r in cur.fetchall()
        }

        # Sponsor total (all rows, no exclusion needed -- sponsors are not email sources)
        cur.execute("""
            SELECT COUNT(*) AS n
            FROM superage.games_landing_events
            WHERE utm_source = ANY(%s)
        """, (SPONSOR_SOURCES,))
        landing_sponsors = safe_int(cur.fetchone()["n"])

        # Sponsor by source -- all configured sponsors, zero-filled
        cur.execute("""
            SELECT utm_source AS source, COUNT(*) AS count
            FROM superage.games_landing_events
            WHERE utm_source = ANY(%s)
            GROUP BY utm_source
        """, (SPONSOR_SOURCES,))
        sponsor_counts = {r["source"]: safe_int(r["count"]) for r in cur.fetchall()}
        landing_by_source_sponsors = sorted(
            [{"source": s, "count": sponsor_counts.get(s, 0)} for s in SPONSOR_SOURCES],
            key=lambda x: -x["count"],
        )

        # Event total -- case-insensitive utm_source match
        cur.execute("""
            SELECT COUNT(*) AS n
            FROM superage.games_landing_events
            WHERE LOWER(TRIM(utm_source)) = ANY(%s)
        """, (EVENT_SOURCES,))
        landing_events_partners = safe_int(cur.fetchone()["n"])

        # Event by source -- all configured events, zero-filled
        cur.execute("""
            SELECT LOWER(TRIM(utm_source)) AS source, COUNT(*) AS count
            FROM superage.games_landing_events
            WHERE LOWER(TRIM(utm_source)) = ANY(%s)
            GROUP BY 1
        """, (EVENT_SOURCES,))
        event_counts = {r["source"]: safe_int(r["count"]) for r in cur.fetchall()}
        landing_by_source_events = sorted(
            [{"source": s, "count": event_counts.get(s, 0)} for s in EVENT_SOURCES],
            key=lambda x: -x["count"],
        )

        # Campaigns from filtered landing events
        cur.execute("""
            SELECT utm_campaign AS campaign, COUNT(*) AS count
            FROM superage.games_landing_events
            WHERE utm_source = ANY(%s)
              AND NOT (utm_source = ANY(%s) AND utm_medium = 'email')
              AND utm_campaign IS NOT NULL AND TRIM(utm_campaign) != ''
            GROUP BY utm_campaign ORDER BY count DESC
        """, (KNOWN_SOURCES, RAW_EMAIL_SOURCES))
        filtered_by_campaign = [
            (r["campaign"], safe_int(r["count"])) for r in cur.fetchall()
        ]

        # Daily counts from filtered landing events
        cur.execute("""
            SELECT date::date AS day, COUNT(*) AS count
            FROM superage.games_landing_events
            WHERE utm_source = ANY(%s)
              AND NOT (utm_source = ANY(%s) AND utm_medium = 'email')
              AND date IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (KNOWN_SOURCES, RAW_EMAIL_SOURCES))
        filtered_by_day = [
            (str(r["day"]), safe_int(r["count"])) for r in cur.fetchall()
        ]

        # Medium counts from filtered landing events
        cur.execute("""
            SELECT utm_medium AS medium, COUNT(*) AS count
            FROM superage.games_landing_events
            WHERE utm_source = ANY(%s)
              AND NOT (utm_source = ANY(%s) AND utm_medium = 'email')
              AND utm_medium IS NOT NULL AND TRIM(utm_medium) != ''
            GROUP BY utm_medium ORDER BY count DESC
        """, (KNOWN_SOURCES, RAW_EMAIL_SOURCES))
        filtered_medium_dict = {
            r["medium"]: safe_int(r["count"])
            for r in cur.fetchall()
        }

        # ════════════════════════════════════════════════════
        # SUPERAGE RAW EMAIL CLICKS
        # ════════════════════════════════════════════════════

        cur.execute("""
            SELECT COUNT(*) AS n
            FROM superage."Campaigns_Clicks"
            WHERE "URL" ILIKE ANY(%s)
        """, (GAME_URL_PATTERNS,))
        sa_total = safe_int(cur.fetchone()["n"])
        logger.info("SA raw email clicks total=%d", sa_total)

        cur.execute("""
            SELECT issue_name AS campaign, COUNT(*) AS count
            FROM superage."Campaigns_Clicks"
            WHERE "URL" ILIKE ANY(%s)
              AND issue_name IS NOT NULL
            GROUP BY issue_name ORDER BY count DESC
        """, (GAME_URL_PATTERNS,))
        sa_by_campaign = [
            (r["campaign"], safe_int(r["count"])) for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT "Date"::date AS day, COUNT(*) AS count
            FROM superage."Campaigns_Clicks"
            WHERE "URL" ILIKE ANY(%s)
              AND "Date" IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (GAME_URL_PATTERNS,))
        sa_by_day = [
            (str(r["day"]), safe_int(r["count"])) for r in cur.fetchall()
        ]

        # ════════════════════════════════════════════════════
        # AGEIST RAW EMAIL CLICKS
        # ════════════════════════════════════════════════════

        cur.execute("""
            SELECT COUNT(*) AS n
            FROM ageist.ageist_clicks
            WHERE COALESCE(final_url, '') ILIKE ANY(%s)
              AND NULLIF(LOWER(TRIM(email_address)), '') IS NOT NULL
        """, (GAME_URL_PATTERNS,))
        ag_total = safe_int(cur.fetchone()["n"])
        logger.info("Ageist raw email clicks total=%d", ag_total)

        cur.execute("""
            SELECT c.campaign_title AS campaign, COUNT(*) AS count
            FROM ageist.ageist_clicks ck
            JOIN ageist.ageist_campaigns c ON c.campaign_id = ck.campaign_id
            WHERE COALESCE(ck.final_url, '') ILIKE ANY(%s)
              AND NULLIF(LOWER(TRIM(ck.email_address)), '') IS NOT NULL
              AND c.campaign_title IS NOT NULL
            GROUP BY c.campaign_title ORDER BY count DESC
        """, (GAME_URL_PATTERNS,))
        ag_by_campaign = [
            (r["campaign"], safe_int(r["count"])) for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT ck.campaign_send_time::date AS day, COUNT(*) AS count
            FROM ageist.ageist_clicks ck
            WHERE COALESCE(ck.final_url, '') ILIKE ANY(%s)
              AND ck.campaign_send_time IS NOT NULL
              AND NULLIF(LOWER(TRIM(ck.email_address)), '') IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (GAME_URL_PATTERNS,))
        ag_by_day = [
            (str(r["day"]), safe_int(r["count"])) for r in cur.fetchall()
        ]

        # ════════════════════════════════════════════════════
        # HEALTHBRIEF RAW EMAIL CLICKS (optimism schema, same DB)
        # ════════════════════════════════════════════════════

        cur.execute("""
            SELECT COUNT(*) AS n
            FROM optimism.healthbrief_contact_activity
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
        """, (GAME_URL_PATTERNS,))
        hb_total = safe_int(cur.fetchone()["n"])
        logger.info("HealthBrief raw email clicks total=%d", hb_total)

        cur.execute("""
            SELECT mailing_name AS campaign, COUNT(*) AS count
            FROM optimism.healthbrief_contact_activity
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND mailing_name IS NOT NULL
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
            GROUP BY mailing_name ORDER BY count DESC
        """, (GAME_URL_PATTERNS,))
        hb_by_campaign = [
            (r["campaign"], safe_int(r["count"])) for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT timestamp::date AS day, COUNT(*) AS count
            FROM optimism.healthbrief_contact_activity
            WHERE type = 'click'
              AND data ILIKE ANY(%s)
              AND timestamp IS NOT NULL
              AND mailing_name NOT ILIKE '%%[TEST]%%'
              AND bot = 'No'
            GROUP BY 1 ORDER BY 1
        """, (GAME_URL_PATTERNS,))
        hb_by_day = [
            (str(r["day"]), safe_int(r["count"])) for r in cur.fetchall()
        ]

    finally:
        cur.close()
        conn.close()

    # ════════════════════════════════════════════════════
    # ALLHEALTHY RAW EMAIL CLICKS (separate DB)
    # ════════════════════════════════════════════════════

    ah_total       = 0
    ah_by_campaign = []
    ah_by_day      = []
    try:
        ah_conn = ah_connection()
        ah_cur  = ah_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            ah_cur.execute("""
                SELECT COUNT(*) AS n
                FROM public.allhealthy_contact_clicks
                WHERE data::text ILIKE ANY(%s)
                  AND bot = 'No'
            """, (GAME_URL_PATTERNS,))
            ah_total = safe_int(ah_cur.fetchone()["n"])

            ah_cur.execute("""
                SELECT mailing_name AS campaign, COUNT(*) AS count
                FROM public.allhealthy_contact_clicks
                WHERE data::text ILIKE ANY(%s)
                  AND mailing_name IS NOT NULL
                  AND bot = 'No'
                GROUP BY mailing_name ORDER BY count DESC
            """, (GAME_URL_PATTERNS,))
            ah_by_campaign = [
                (r["campaign"], safe_int(r["count"])) for r in ah_cur.fetchall()
            ]

            ah_cur.execute("""
                SELECT event_timestamp::date AS day, COUNT(*) AS count
                FROM public.allhealthy_contact_clicks
                WHERE data::text ILIKE ANY(%s)
                  AND event_timestamp IS NOT NULL
                  AND bot = 'No'
                GROUP BY 1 ORDER BY 1
            """, (GAME_URL_PATTERNS,))
            ah_by_day = [
                (str(r["day"]), safe_int(r["count"])) for r in ah_cur.fetchall()
            ]

            logger.info("AllHealthy raw email clicks OK -- total=%d", ah_total)
        finally:
            ah_cur.close()
            ah_conn.close()
    except Exception as e:
        logger.warning(
            "AllHealthy raw clicks unavailable (%s). AH metrics will be zero.", e
        )

    # ════════════════════════════════════════════════════
    # COMBINE ALL LANDING METRICS
    # ════════════════════════════════════════════════════

    total_landing = sa_total + ah_total + ag_total + hb_total + filtered_landing_total

    sa_website_from_landing = landing_source_from_events.get("superage", 0)
    brand_source_dict: dict = defaultdict(int)
    for src, cnt in landing_source_from_events.items():
        if src in OUR_BRAND_SOURCES and src != "superage":
            brand_source_dict[src] += cnt
    brand_source_dict["superage (campaigns)"] += sa_total
    brand_source_dict["superage (website)"]   += sa_website_from_landing
    brand_source_dict["allhealthy"]  += ah_total
    brand_source_dict["ageist"]      += ag_total
    brand_source_dict["healthbrief"] += hb_total

    landing_by_source_brands = [
        {"source": k, "count": v}
        for k, v in sorted(brand_source_dict.items(), key=lambda x: -x[1])
        if v > 0
    ]

    landing_our_brands = sum(brand_source_dict.values())

    campaign_dict: dict = defaultdict(int)
    for camp, cnt in sa_by_campaign:
        campaign_dict[camp] += cnt
    for camp, cnt in ah_by_campaign:
        campaign_dict[camp] += cnt
    for camp, cnt in ag_by_campaign:
        campaign_dict[camp] += cnt
    for camp, cnt in hb_by_campaign:
        campaign_dict[camp] += cnt
    for camp, cnt in filtered_by_campaign:
        campaign_dict[camp] += cnt
    landing_by_campaign = [
        {"campaign": k, "count": v}
        for k, v in sorted(campaign_dict.items(), key=lambda x: -x[1])
    ][:10]

    day_dict: dict = defaultdict(int)
    for d, cnt in sa_by_day:
        day_dict[d] += cnt
    for d, cnt in ah_by_day:
        day_dict[d] += cnt
    for d, cnt in ag_by_day:
        day_dict[d] += cnt
    for d, cnt in hb_by_day:
        day_dict[d] += cnt
    for d, cnt in filtered_by_day:
        day_dict[d] += cnt
    landing_by_day = [
        {"day": k, "count": v}
        for k, v in sorted(day_dict.items())
    ]

    raw_brand_email_total = sa_total + ah_total + ag_total + hb_total
    filtered_medium_dict["email"] = (
        filtered_medium_dict.get("email", 0) + raw_brand_email_total
    )
    landing_by_medium = [
        {"medium": k, "count": v}
        for k, v in sorted(filtered_medium_dict.items(), key=lambda x: -x[1])
    ][:10]

    direct_buyers   = (
        (total_tickets - waitlist_buyers) if waitlist_buyers is not None else None
    )
    conversion_rate = (
        round(100.0 * waitlist_buyers / total_waitlist, 1)
        if waitlist_buyers and total_waitlist else None
    )

    M = {
        "_note":      "Auto-generated by sales_metrics_lambda. Do not edit manually.",
        "data_as_of": datetime.now(ZoneInfo("America/New_York")).strftime(
            "%b %-d, %Y %I:%M %p EST"
        ),

        "total_tickets":   total_tickets,
        "total_waitlist":  total_waitlist,
        "waitlist_buyers": waitlist_buyers,
        "direct_buyers":   direct_buyers,
        "conversion_rate": f"{conversion_rate}%" if conversion_rate is not None else None,

        "landing_events":           total_landing,
        "landing_our_brands":       landing_our_brands,
        "landing_sponsors":         landing_sponsors,
        "landing_events_partners":  landing_events_partners,
        "estimated_revenue":        estimated_revenue,

        "funnel": [
            {"label": "Landing Events",   "count": total_landing,  "sub": "Clicks from all known sources"},
            {"label": "Waitlist Signups", "count": total_waitlist, "sub": "Valid, unsuppressed emails"},
            {"label": "Ticket Purchases", "count": total_tickets,  "sub": "Confirmed purchases"},
        ],

        "ticket_types": ticket_types,

        "ticket_waitlist_overlap": {
            "on_waitlist":     waitlist_buyers,
            "not_on_waitlist": direct_buyers,
        },

        "age_distribution":    age_distribution,
        "gender_distribution": gender_distribution,
        "city_distribution":   city_distribution,

        "landing_by_source_brands":   landing_by_source_brands,
        "landing_by_source_sponsors": landing_by_source_sponsors,
        "landing_by_source_events":   landing_by_source_events,
        "landing_by_campaign":        landing_by_campaign,
        "landing_by_medium":          landing_by_medium,
        "landing_by_day":             landing_by_day,

        "recent_tickets": {
            "columns": list(recent_rows[0].keys()) if recent_rows else [],
            "rows":    recent_rows,
        },

        "persona":       persona,
        "ticket_funnel": ticket_funnel,

        "_column_map": {
            "tickets_email":  t_email,
            "tickets_oid":    t_oid,
            "tickets_date":   t_date,
            "tickets_type":   t_type,
            "tickets_dob":    t_dob,
            "tickets_gender": t_gender,
            "tickets_city":   t_city,
        },

        "_source_totals": {
            "sa_raw_email":              sa_total,
            "sa_website_from_landing":   sa_website_from_landing,
            "superage_campaigns_bucket": sa_total,
            "superage_website_bucket":   sa_website_from_landing,
            "ah_raw_email":              ah_total,
            "ag_raw_email":              ag_total,
            "hb_raw_email":              hb_total,
            "filtered_landing_total":    filtered_landing_total,
            "landing_sponsors":          landing_sponsors,
            "landing_events_partners":   landing_events_partners,
            "check_total":               sa_total + ah_total + ag_total + hb_total + filtered_landing_total,
        },
    }

    body = json.dumps(M, indent=2, default=str)
    r2_result = write_to_r2(body)

    logger.info(
        "Done -- tickets=%d waitlist=%d "
        "landing=%d (sa_raw=%d sa_web=%d ah=%d ag=%d hb=%d filtered=%d sponsors=%d events=%d) r2_key=%s",
        total_tickets, total_waitlist,
        total_landing, sa_total, sa_website_from_landing,
        ah_total, ag_total, hb_total, filtered_landing_total, landing_sponsors,
        landing_events_partners, R2_FILE_PATH,
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status":                   "ok",
            "data_as_of":               M["data_as_of"],
            "total_tickets":            total_tickets,
            "total_waitlist":           total_waitlist,
            "landing_events":           total_landing,
            "landing_our_brands":       landing_our_brands,
            "landing_sponsors":         landing_sponsors,
            "landing_events_partners":  landing_events_partners,
            "r2":                       r2_result,
            "_source_totals":           M["_source_totals"],
        }),
    }