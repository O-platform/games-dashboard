import os
import re
import json
import boto3
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from boto3.dynamodb.conditions import Attr

# -------------------------
# Config from environment
# -------------------------
SECRET_ARN   = os.environ["RDS_SECRET_ARN"]
DYNAMO_TABLE = os.environ.get("TABLE_NAME", "email_logs_superage")
DB_SCHEMA    = os.environ.get("DB_SCHEMA", "superage")
DB_TABLE     = os.environ.get("DB_TABLE", "awe_course_checkout_landing_events")
SSM_KEY      = os.environ.get("SSM_KEY", "/awe_landing_sync/last_run")

# Sync records whose o_event matches this value...
O_EVENT      = os.environ.get("O_EVENT", "awe_course_checkout_redirect")
# ...OR whose utm_campaign matches this value (Google Ads traffic for the AWE
# course). The main-page-landing rows have NO o_event, so they are matched by
# campaign; the Google Ads checkout redirects carry both o_event AND this campaign.
GOOGLEADS_CAMPAIGN = os.environ.get("GOOGLEADS_CAMPAIGN", "google_ads_awe")

# Fallback hours back on very first run
FIRST_RUN_HOURS_BACK = int(os.environ.get("FIRST_RUN_HOURS_BACK", "72"))

# -------------------------
# AWS clients
# -------------------------
dynamodb = boto3.resource("dynamodb")
ssm      = boto3.client("ssm")
sm       = boto3.client("secretsmanager")

# -------------------------
# SSM — last run tracking
# (same incremental mechanism as Games-landing-sync-Dynamo-to-RDS,
#  with its own SSM key so it tracks independently)
# -------------------------
def get_last_run() -> str:
    try:
        resp = ssm.get_parameter(Name=SSM_KEY)
        return resp["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        fallback = (datetime.utcnow() - timedelta(hours=FIRST_RUN_HOURS_BACK)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[sync] No SSM key found — first run, scanning from {fallback}")
        return fallback

def save_last_run(ts: str):
    ssm.put_parameter(Name=SSM_KEY, Value=ts, Type="String", Overwrite=True)
    print(f"[sync] SSM last_run saved → {ts}")

# -------------------------
# Secrets Manager
# -------------------------
def get_db_credentials() -> dict:
    resp   = sm.get_secret_value(SecretId=SECRET_ARN)
    secret = json.loads(resp["SecretString"])
    return {
        "host":     secret["host"],
        "port":     int(secret.get("port", 5432)),
        "dbname":   secret["dbname"],
        "user":     secret["username"],
        "password": secret["password"],
    }

# -------------------------
# DynamoDB helpers
# -------------------------
def _matches(item: dict) -> bool:
    """A record we care about: the checkout redirect o_event OR a Google Ads
    landing (matched by utm_campaign, since those rows carry no o_event)."""
    return (item.get("o_event") == O_EVENT
            or item.get("utm_campaign") == GOOGLEADS_CAMPAIGN)

def scan_dynamo(since: str = None) -> list:
    """
    Scan email_logs_superage for records where o_event == O_EVENT
    OR utm_campaign == GOOGLEADS_CAMPAIGN.
    If `since` is provided, also filters by date >= since (incremental).
    If None, returns ALL matching records (used for backfill).
    """
    table    = dynamodb.Table(DYNAMO_TABLE)
    items    = []
    last_key = None

    # o_event OR utm_campaign — Google Ads landings have no o_event
    match_expr = Attr("o_event").eq(O_EVENT) | Attr("utm_campaign").eq(GOOGLEADS_CAMPAIGN)

    while True:
        filter_expr = match_expr
        if since:
            filter_expr = match_expr & Attr("date").gte(since)

        kwargs = {"FilterExpression": filter_expr}
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key

        resp     = table.scan(**kwargs)
        items   += resp.get("Items", [])
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break

    # Safety net in case anything non-matching slips through
    return [i for i in items if _matches(i)]

# -------------------------
# RDS helpers
# -------------------------
CREATE_TABLE_SQL = """
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{table} (
    id              TEXT PRIMARY KEY,
    date            TIMESTAMP,
    email           TEXT,
    utm_source      TEXT,
    utm_medium      TEXT,
    utm_campaign    TEXT,
    oid             TEXT,
    o_event         TEXT,
    product_url     TEXT,
    user_agent      TEXT,
    synced_at       TIMESTAMP DEFAULT NOW()
);
"""

# Adds user_agent column safely if the table already exists without it
ALTER_TABLE_SQL = """
ALTER TABLE {schema}.{table}
    ADD COLUMN IF NOT EXISTS user_agent TEXT;
"""

UPSERT_SQL = """
INSERT INTO {schema}.{table}
    (id, date, email, utm_source, utm_medium, utm_campaign, oid, o_event, product_url, user_agent)
VALUES
    (%(id)s, %(date)s, %(email)s, %(utm_source)s, %(utm_medium)s,
     %(utm_campaign)s, %(oid)s, %(o_event)s, %(product_url)s, %(user_agent)s)
ON CONFLICT (id) DO NOTHING;
"""

BACKFILL_USER_AGENT_SQL = """
UPDATE {schema}.{table}
SET    user_agent = %(user_agent)s
WHERE  id = %(id)s
AND    user_agent IS NULL;
"""

def parse_date(val: str):
    if not val:
        return None
    try:
        return datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def clean_utm_campaign(val: str) -> str:
    if not val:
        return val
    return re.sub(r'[^\w]+$', '', val).strip()

def build_row(item: dict) -> dict:
    return {
        "id":           item.get("id"),
        "date":         parse_date(item.get("date")),
        "email":        item.get("email"),
        "utm_source":   item.get("utm_source"),
        "utm_medium":   item.get("utm_medium"),
        "utm_campaign": clean_utm_campaign(item.get("utm_campaign")),
        "oid":          item.get("oid"),
        "o_event":      item.get("o_event"),
        "product_url":  item.get("product_url"),
        "user_agent":   item.get("user_agent"),
    }

def sync_to_rds(rows: list, creds: dict) -> int:
    conn = psycopg2.connect(**creds)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL.format(schema=DB_SCHEMA, table=DB_TABLE))
                cur.execute(ALTER_TABLE_SQL.format(schema=DB_SCHEMA, table=DB_TABLE))
                sql = UPSERT_SQL.format(schema=DB_SCHEMA, table=DB_TABLE)
                psycopg2.extras.execute_batch(cur, sql, rows, page_size=100)
        return len(rows)
    finally:
        conn.close()

def backfill_user_agent(rows: list, creds: dict) -> int:
    """
    For every DynamoDB record that has a user_agent value,
    UPDATE the matching RDS row only if user_agent is currently NULL.
    """
    rows_with_ua = [
        {"id": r["id"], "user_agent": r["user_agent"]}
        for r in rows
        if r.get("user_agent")
    ]

    if not rows_with_ua:
        print("[backfill] No rows with user_agent found in DynamoDB — nothing to update")
        return 0

    conn = psycopg2.connect(**creds)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ALTER_TABLE_SQL.format(schema=DB_SCHEMA, table=DB_TABLE))
                sql = BACKFILL_USER_AGENT_SQL.format(schema=DB_SCHEMA, table=DB_TABLE)
                psycopg2.extras.execute_batch(cur, sql, rows_with_ua, page_size=100)
        print(f"[backfill] Updated up to {len(rows_with_ua)} rows with user_agent")
        return len(rows_with_ua)
    finally:
        conn.close()

# -------------------------
# Lambda handler
# -------------------------
def lambda_handler(event, context):
    """
    Incremental sync of email_logs_superage rows where
      o_event == O_EVENT  OR  utm_campaign == GOOGLEADS_CAMPAIGN
    into DB_SCHEMA.DB_TABLE (default superage.awe_course_checkout_landing_events).
    Incremental state tracked in SSM (SSM_KEY) — same mechanism as Games-landing-sync.

    Google Ads landings (utm_campaign=googleads_awe_course, utm_medium=website)
    are ingested by the SAME logic as the checkout-redirect rows; they carry no
    o_event, so they are matched by campaign instead.

    Manual trigger options:
      { "since": "2026-01-01 00:00:00" }   ← override start time
      { "force_full": true }               ← ignore SSM, scan from FIRST_RUN_HOURS_BACK
      { "backfill_user_agent": true }      ← scan ALL matching records, backfill user_agent on existing rows
    """
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    event   = event or {}

    # ------------------------------------------------------------------
    # BACKFILL MODE — update user_agent on all existing RDS rows
    # ------------------------------------------------------------------
    if event.get("backfill_user_agent"):
        print(f"[backfill] Scanning ALL matching records (o_event={O_EVENT} OR "
              f"utm_campaign={GOOGLEADS_CAMPAIGN}) for user_agent backfill")
        items = scan_dynamo(since=None)
        print(f"[backfill] Found {len(items)} total matching records in DynamoDB")

        if not items:
            return {"statusCode": 200, "message": "No records found in DynamoDB", "updated": 0}

        creds   = get_db_credentials()
        rows    = [build_row(i) for i in items]
        updated = backfill_user_agent(rows, creds)

        return {
            "statusCode":     200,
            "mode":           "backfill_user_agent",
            "o_event":        O_EVENT,
            "googleads_campaign": GOOGLEADS_CAMPAIGN,
            "dynamo_scanned": len(items),
            "rds_updated":    updated,
        }

    # ------------------------------------------------------------------
    # NORMAL INCREMENTAL SYNC
    # ------------------------------------------------------------------
    if event.get("force_full"):
        since = (datetime.utcnow() - timedelta(hours=FIRST_RUN_HOURS_BACK)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[sync] force_full=true — scanning from {since}")
    elif event.get("since"):
        since = event["since"]
        print(f"[sync] Manual since override → {since}")
    else:
        since = get_last_run()

    print(f"[sync] Window: {since} → {now_str} (o_event={O_EVENT} OR utm_campaign={GOOGLEADS_CAMPAIGN})")

    items = scan_dynamo(since)
    print(f"[sync] Found {len(items)} new matching records")

    if not items:
        save_last_run(now_str)
        return {
            "statusCode": 200,
            "inserted":   0,
            "since":      since,
            "until":      now_str,
            "message":    "No new records",
        }

    creds    = get_db_credentials()
    rows     = [build_row(i) for i in items]
    inserted = sync_to_rds(rows, creds)
    print(f"[sync] Inserted {inserted} rows into {DB_SCHEMA}.{DB_TABLE}")

    save_last_run(now_str)

    return {
        "statusCode": 200,
        "inserted":   inserted,
        "since":      since,
        "until":      now_str,
        "table":      f"{DB_SCHEMA}.{DB_TABLE}",
    }
