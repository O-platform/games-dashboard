"""
superage_opens_ingest_lambda.py
================================
Ingests raw Ongage open events into superage."Campaigns_Opens".

Modes (pass in the Lambda event payload):
  { "mode": "full" }          — backfill last FULL_DAYS (default 120) days.
                                Self-invokes asynchronously per daily chunk.
  { "mode": "full",
    "cursor": "YYYY-MM-DD" }  — resume full-mode from this date (set by
                                each self-invocation).
  { "mode": "incremental" }   — fetch from MAX(opened_at) in the table to
                                yesterday. Safe to run daily via EventBridge.
  {}  / omitted               — defaults to incremental.

Environment variables (required unless noted):
  DB_SECRET_ARN   — Secrets Manager ARN: {host,port,dbname,username,password}
  ONGAGE_SECRET_ARN — Secrets Manager ARN: {api_key, username, account_code, base_url}
  SA_SCHEMA       — Postgres schema name (default: superage)
  AWS_REGION      — AWS region (default: us-east-1)
  FULL_DAYS       — days to backfill in full mode (default: 120)
  CHUNK_DAYS      — days processed per Lambda invocation (default: 7)
  ONGAGE_LIST_ID  — Ongage list/account list id (required)

IAM: the Lambda's execution role needs lambda:InvokeFunction on itself
     (see iam_opens_ingest_policy.json).

Runtime: Python 3.12 | boto3 built-in | requests via Lambda layer or inline
"""

import json
import logging
import os
from datetime import date, datetime, timedelta

import boto3
import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Constants ────────────────────────────────────────────────────────────────
S           = os.environ.get("SA_SCHEMA", "superage")
FULL_DAYS   = int(os.environ.get("FULL_DAYS",  "120"))
CHUNK_DAYS  = int(os.environ.get("CHUNK_DAYS", "7"))
LIST_ID     = os.environ.get("ONGAGE_LIST_ID", "")
PAGE_SIZE   = 500      # Ongage max rows per page

# ── Secret caches ────────────────────────────────────────────────────────────
_db_secret     = None
_ongage_secret = None


def _sm_client():
    return boto3.client("secretsmanager",
                        region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _get_db_secret():
    global _db_secret
    if _db_secret is None:
        _db_secret = json.loads(
            _sm_client().get_secret_value(
                SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
        logger.info("DB secret fetched.")
    return _db_secret


def _get_ongage_secret():
    global _ongage_secret
    if _ongage_secret is None:
        _ongage_secret = json.loads(
            _sm_client().get_secret_value(
                SecretId=os.environ["ONGAGE_SECRET_ARN"])["SecretString"])
        logger.info("Ongage secret fetched.")
    return _ongage_secret


# ── DB connection ────────────────────────────────────────────────────────────
def get_connection():
    s = _get_db_secret()
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", s["host"]),
        port=int(os.environ.get("DB_PORT", s.get("port", 5432))),
        dbname=os.environ.get("DB_NAME", s["dbname"]),
        user=os.environ.get("DB_USER", s["username"]),
        password=s["password"],
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=30,
    )


# ── Ongage API helper ────────────────────────────────────────────────────────
def _ongage_headers():
    s = _get_ongage_secret()
    return {
        "X_USERNAME":     s["username"],
        "X_PASSWORD":     s["api_key"],
        "X_ACCOUNT_CODE": s["account_code"],
        "Content-Type":   "application/json",
    }


def _ongage_base():
    s = _get_ongage_secret()
    return s.get("base_url", "https://api.ongage.net").rstrip("/")


def fetch_opens_for_day(day: date) -> list[dict]:
    """
    Fetch all open events for a single calendar day from Ongage.
    Returns list of dicts with keys:
      email, campaign_id, campaign_name, opened_at, list_id
    """
    from_ts = int(datetime(day.year, day.month, day.day, 0,  0,  0).timestamp())
    to_ts   = int(datetime(day.year, day.month, day.day, 23, 59, 59).timestamp())

    base    = _ongage_base()
    headers = _ongage_headers()
    results = []
    page    = 1

    while True:
        url = (
            f"{base}/api/contacts/subscribers/activity"
            f"?activity_type=open"
            f"&from_date={from_ts}"
            f"&to_date={to_ts}"
            f"&list_id={LIST_ID}"
            f"&index={page}"
            f"&count={PAGE_SIZE}"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ongage API error for {day}: {e}")
            raise

        body = resp.json()
        # Ongage wraps data in {"payload": {"data": [...], "total_count": N}}
        payload  = body.get("payload", {})
        rows     = payload.get("data", [])
        total    = int(payload.get("total_count", 0))

        for r in rows:
            # Ongage open-activity fields — adjust key names if your
            # account uses different field names
            ts = r.get("activity_timestamp") or r.get("open_time") or r.get("date")
            try:
                opened_at = datetime.utcfromtimestamp(int(ts)) if ts else datetime.combine(day, datetime.min.time())
            except (ValueError, TypeError):
                opened_at = datetime.combine(day, datetime.min.time())

            results.append({
                "email":         (r.get("email") or r.get("subscriber_email") or "").lower().strip(),
                "campaign_id":   r.get("campaign_id") or r.get("message_id"),
                "campaign_name": r.get("campaign_name") or r.get("message_name"),
                "opened_at":     opened_at,
                "list_id":       r.get("list_id") or LIST_ID,
            })

        logger.info(f"{day}: page {page}, got {len(rows)}, total={total}")

        if len(results) >= total or len(rows) < PAGE_SIZE:
            break
        page += 1

    return results


# ── DB operations ────────────────────────────────────────────────────────────
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}."Campaigns_Opens" (
    id            BIGSERIAL    PRIMARY KEY,
    email         VARCHAR(320) NOT NULL,
    campaign_id   BIGINT,
    campaign_name TEXT,
    opened_at     TIMESTAMPTZ  NOT NULL,
    list_id       VARCHAR(64),
    created_at    TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_camps_opens_email
    ON {schema}."Campaigns_Opens" (email);
CREATE INDEX IF NOT EXISTS idx_camps_opens_opened
    ON {schema}."Campaigns_Opens" (opened_at);
CREATE INDEX IF NOT EXISTS idx_camps_opens_campaign
    ON {schema}."Campaigns_Opens" (campaign_id);
"""

UPSERT_SQL = """
INSERT INTO {schema}."Campaigns_Opens"
    (email, campaign_id, campaign_name, opened_at, list_id)
VALUES %s
ON CONFLICT DO NOTHING
"""

# Unique constraint needed for ON CONFLICT DO NOTHING to deduplicate properly.
# Run once manually after table creation:
#   ALTER TABLE superage."Campaigns_Opens"
#   ADD CONSTRAINT uq_camps_opens
#   UNIQUE (email, campaign_id, opened_at);


def ensure_table(cur):
    cur.execute(CREATE_TABLE_SQL.format(schema=S))


def get_max_opened_at(cur) -> date | None:
    cur.execute(f'SELECT MAX(opened_at)::date FROM {S}."Campaigns_Opens"')
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def upsert_opens(cur, rows: list[dict]):
    if not rows:
        return 0
    values = [
        (r["email"], r["campaign_id"], r["campaign_name"],
         r["opened_at"], r["list_id"])
        for r in rows
        if r.get("email")
    ]
    psycopg2.extras.execute_values(
        cur,
        UPSERT_SQL.format(schema=S),
        values,
        page_size=500,
    )
    return len(values)


# ── Self-invocation ──────────────────────────────────────────────────────────
def self_invoke(context, next_cursor: date, end_date: date):
    """Fire-and-forget: invoke self asynchronously for the next chunk."""
    payload = {
        "mode":     "full",
        "cursor":   next_cursor.isoformat(),
        "end_date": end_date.isoformat(),
    }
    boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1")).invoke(
        FunctionName=context.function_name,
        InvocationType="Event",          # async — does not wait
        Payload=json.dumps(payload).encode(),
    )
    logger.info(f"Self-invoked for cursor={next_cursor}, end={end_date}")


# ── Handler ──────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    mode      = event.get("mode", "incremental")
    yesterday = date.today() - timedelta(days=1)

    conn = get_connection()
    cur  = conn.cursor()
    ensure_table(cur)
    conn.commit()

    # ── Determine date range for this invocation ─────────────────────────────
    if mode == "full":
        raw_cursor = event.get("cursor")
        raw_end    = event.get("end_date")

        start_date = (date.fromisoformat(raw_cursor)
                      if raw_cursor else date.today() - timedelta(days=FULL_DAYS))
        end_date   = (date.fromisoformat(raw_end)
                      if raw_end   else yesterday)

        # This invocation processes [start_date, chunk_end]
        chunk_end = min(start_date + timedelta(days=CHUNK_DAYS - 1), end_date)
        logger.info(f"FULL mode: processing {start_date} → {chunk_end} (overall end: {end_date})")

    else:  # incremental
        max_date = get_max_opened_at(cur)
        start_date = (max_date + timedelta(days=1)) if max_date else (yesterday - timedelta(days=7))
        chunk_end  = yesterday
        end_date   = yesterday
        logger.info(f"INCREMENTAL mode: processing {start_date} → {chunk_end}")

    # ── Process each day in the chunk ────────────────────────────────────────
    total_inserted = 0
    d = start_date
    while d <= chunk_end:
        logger.info(f"Fetching opens for {d} ...")
        rows = fetch_opens_for_day(d)
        inserted = upsert_opens(cur, rows)
        conn.commit()
        logger.info(f"{d}: inserted {inserted} opens (fetched {len(rows)})")
        total_inserted += inserted
        d += timedelta(days=1)

    cur.close()
    conn.close()

    # ── Self-invoke for next chunk if full mode and not done ─────────────────
    next_cursor = chunk_end + timedelta(days=1)
    if mode == "full" and next_cursor <= end_date:
        self_invoke(context, next_cursor, end_date)
        remaining_days = (end_date - next_cursor).days + 1
        logger.info(f"Queued next chunk. {remaining_days} days remaining.")
    else:
        logger.info("All done — no more chunks to process.")

    return {
        "statusCode": 200,
        "mode":       mode,
        "chunk":      f"{start_date} → {chunk_end}",
        "inserted":   total_inserted,
        "done":       next_cursor > end_date,
    }
