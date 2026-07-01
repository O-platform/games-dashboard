"""
superage_opens_ingest_lambda.py
================================
Ingests raw Campaign Monitor open events into superage.campaign_opens.

Mirrors CampaignClicksToRDS exactly — same secrets, same auditing table,
same pagination pattern. Uses CM /opens.json endpoint instead of /clicks.json.

Modes (pass in the Lambda event payload):
  {}                              — incremental: fetch from last audit time
  { "mode": "full" }             — backfill FULL_DAYS (default 120) from today
  { "mode": "full",
    "cursor": "YYYY-MM-DD" }     — resume full-mode from this date (self-invoked)

Environment variables (all optional — defaults shown):
  FULL_DAYS   — days to backfill in full mode  (default: 120)
  CHUNK_DAYS  — days per self-invocation chunk (default: 7)

Secrets (same as existing lambdas):
  CampaignMonitorCredientials  — {API_KEY, CLIENT_ID}
  DBMetrics_Superage_Secrets   — {host, dbname, username, password, port}
"""

import json
import time
import boto3
import requests
import psycopg2
from datetime import datetime, timedelta, date


# ── Config ───────────────────────────────────────────────────────────────────
CM_SECRET_ID = "arn:aws:secretsmanager:us-west-1:550130133458:secret:CampaignMonitorCredientials-jEKwKX"
DB_SECRET_ID = "arn:aws:secretsmanager:us-west-1:550130133458:secret:DBMetrics_Superage_Secrets-fnwNnN"

FULL_DAYS    = int(__import__('os').environ.get("FULL_DAYS",  "120"))
CHUNK_DAYS   = int(__import__('os').environ.get("CHUNK_DAYS", "7"))
PAGE_SIZE    = 1000
FUNCTION_NAME = "CampaignOpensToRDS"   # used in SA_Auditing


# ── Secrets ───────────────────────────────────────────────────────────────────
def get_cm_secrets():
    client = boto3.client("secretsmanager", region_name="us-west-1")
    response = client.get_secret_value(SecretId=CM_SECRET_ID)
    s = json.loads(response["SecretString"])
    return s["API_KEY"], s["CLIENT_ID"]


def get_db_config():
    client = boto3.client("secretsmanager", region_name="us-west-1")
    response = client.get_secret_value(SecretId=DB_SECRET_ID)
    s = json.loads(response["SecretString"])
    return {
        "host":     s["host"],
        "database": s.get("dbname", s.get("database", "postgres")),
        "user":     s["username"],
        "password": s["password"],
        "port":     int(s.get("port", 5432)),
    }


# ── DB helpers ────────────────────────────────────────────────────────────────
def get_connection():
    return psycopg2.connect(**get_db_config())


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS superage.campaign_opens (
                id               BIGSERIAL    PRIMARY KEY,
                email_address    VARCHAR(320) NOT NULL,
                list_id          VARCHAR(100),
                opened_at        TIMESTAMPTZ,
                ip_address       VARCHAR(50),
                latitude         DOUBLE PRECISION,
                longitude        DOUBLE PRECISION,
                city             VARCHAR(100),
                region           VARCHAR(100),
                country_code     VARCHAR(10),
                country_name     VARCHAR(100),
                campaign_id      VARCHAR(100) NOT NULL,
                campaign_name    TEXT,
                campaign_sent_date DATE,
                created_at       TIMESTAMPTZ  DEFAULT NOW(),
                CONSTRAINT uq_campaign_opens UNIQUE (email_address, campaign_id)
            );

            CREATE INDEX IF NOT EXISTS idx_camp_opens_email
                ON superage.campaign_opens (email_address);

            CREATE INDEX IF NOT EXISTS idx_camp_opens_opened
                ON superage.campaign_opens (opened_at);

            CREATE INDEX IF NOT EXISTS idx_camp_opens_campaign
                ON superage.campaign_opens (campaign_id);
        """)
    conn.commit()


def write_audit(conn, status_code, message):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO superage."SA_Auditing"
                    (function_name, status_code, status_message, execution_time)
                VALUES (%s, %s, %s, %s)
            """, (FUNCTION_NAME, status_code, message[:500], datetime.utcnow()))
        conn.commit()
    except Exception as e:
        print(f"[Audit Failed] {e}")


def get_last_audit_time(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(execution_time)
                FROM superage."SA_Auditing"
                WHERE function_name = %s
            """, (FUNCTION_NAME,))
            result = cur.fetchone()
            if result and result[0]:
                return (result[0] - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        print(f"[Audit time fallback] {e}")
    return datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M")


# ── Campaign Monitor API ──────────────────────────────────────────────────────
def get_campaign_opens(auth, campaign_id, page=1, page_size=PAGE_SIZE, date_filter=None):
    url = f"https://api.createsend.com/api/v3.3/campaigns/{campaign_id}/opens.json"
    params = f"?page={page}&pagesize={page_size}"
    if date_filter:
        params += f"&date={date_filter}"
    try:
        resp = requests.get(url + params, auth=auth, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"API Error for campaign {campaign_id}: {e}")
        return None


# ── Core processing ───────────────────────────────────────────────────────────
def process_campaign_opens(conn, auth, campaign_id, campaign_name, campaign_sent_date, date_filter):
    cur = conn.cursor()
    total_processed = 0
    page = 1

    first_page = get_campaign_opens(auth, campaign_id, page=1,
                                    page_size=PAGE_SIZE, date_filter=date_filter)
    if not first_page:
        return 0

    total_records = first_page.get("NumberOfRecords", 0)
    print(f"  Campaign {campaign_id} ({campaign_name}): {total_records} opens since {date_filter}")

    while True:
        data = get_campaign_opens(auth, campaign_id, page=page,
                                   page_size=PAGE_SIZE, date_filter=date_filter)
        if not data or not data.get("Results"):
            break

        values = []
        for o in data["Results"]:
            try:
                opened_at = (
                    datetime.strptime(o["Date"], "%Y-%m-%d %H:%M:%S")
                    if o.get("Date") else None
                )
                values.append((
                    (o.get("EmailAddress") or "").lower().strip(),
                    o.get("ListID"),
                    opened_at,
                    o.get("IPAddress"),
                    float(o["Latitude"])  if o.get("Latitude")  not in (None, "") else None,
                    float(o["Longitude"]) if o.get("Longitude") not in (None, "") else None,
                    o.get("City"),
                    o.get("Region"),
                    o.get("CountryCode"),
                    o.get("CountryName"),
                    campaign_id,
                    campaign_name,
                    campaign_sent_date,
                ))
            except Exception as e:
                print(f"  Error formatting open row: {e}")
                continue

        if values:
            try:
                cur.executemany("""
                    INSERT INTO superage.campaign_opens (
                        email_address, list_id, opened_at, ip_address,
                        latitude, longitude, city, region,
                        country_code, country_name,
                        campaign_id, campaign_name, campaign_sent_date
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (email_address, campaign_id) DO NOTHING
                """, values)
                conn.commit()
                total_processed += len(values)
                print(f"  Page {page}: {total_processed}/{total_records} inserted")
            except Exception as e:
                print(f"  DB error on page {page}: {e}")
                conn.rollback()
                break

        if len(data["Results"]) < PAGE_SIZE:
            break
        page += 1

    cur.close()
    return total_processed


# ── Self-invocation (full mode) ───────────────────────────────────────────────
def self_invoke(context, next_cursor: str, end_date: str):
    payload = {"mode": "full", "cursor": next_cursor, "end_date": end_date}
    boto3.client("lambda", region_name="us-west-1").invoke(
        FunctionName=context.function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )
    print(f"Self-invoked: cursor={next_cursor}, end={end_date}")


# ── Handler ───────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    conn = None
    try:
        API_KEY, CLIENT_ID = get_cm_secrets()
        AUTH = (API_KEY, "x")
        conn = get_connection()
        ensure_table(conn)

        mode = event.get("mode", "incremental")

        # ── Date range for this invocation ────────────────────────────────────
        if mode == "full":
            yesterday  = (datetime.utcnow() - timedelta(days=1)).date()
            raw_end    = event.get("end_date", yesterday.isoformat())
            raw_cursor = event.get("cursor",
                                   (datetime.utcnow() - timedelta(days=FULL_DAYS)).date().isoformat())

            chunk_start = date.fromisoformat(raw_cursor)
            overall_end = date.fromisoformat(raw_end)
            chunk_end   = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), overall_end)

            # date_filter for CM API: start of chunk window
            date_filter = chunk_start.strftime("%Y-%m-%d %H:%M")
            print(f"FULL mode: {chunk_start} → {chunk_end} (overall end: {overall_end})")
        else:
            date_filter = get_last_audit_time(conn)
            print(f"INCREMENTAL mode: pulling opens since {date_filter}")

        # ── Fetch eligible campaigns in the window ────────────────────────────
        with conn.cursor() as cur:
            if mode == "full":
                cur.execute("""
                    SELECT "CampaignID", "Campaign Name", "Sent Date "::date
                    FROM superage."Campaigns"
                    WHERE "Sent Date "::date BETWEEN %s AND %s
                      AND "Recipients" > 95
                    ORDER BY "Sent Date " DESC
                """, (chunk_start, chunk_end))
            else:
                cur.execute("""
                    SELECT "CampaignID", "Campaign Name", "Sent Date "::date
                    FROM superage."Campaigns"
                    WHERE "Sent Date " >= CURRENT_DATE - INTERVAL '30 days'
                      AND "Recipients" > 95
                    ORDER BY "Sent Date " DESC
                """)
            campaigns = cur.fetchall()

        print(f"Campaigns to process: {len(campaigns)}")
        if not campaigns:
            msg = f"No eligible campaigns in window ({date_filter})"
            write_audit(conn, 200, msg)
            return {"statusCode": 200, "body": msg}

        # ── Process each campaign ─────────────────────────────────────────────
        total_processed = 0
        start_time = time.time()
        max_runtime = 480   # leave headroom before Lambda 15-min limit

        for campaign_id, campaign_name, campaign_sent_date in campaigns:
            if time.time() - start_time > max_runtime:
                print("⏳ Runtime limit approaching — stopping early")
                break
            processed = process_campaign_opens(
                conn, AUTH, campaign_id, campaign_name, campaign_sent_date, date_filter
            )
            total_processed += processed

        # ── Self-invoke next chunk if full mode isn't done ────────────────────
        if mode == "full":
            next_cursor = chunk_end + timedelta(days=1)
            if next_cursor <= overall_end:
                self_invoke(context, next_cursor.isoformat(), overall_end.isoformat())

        msg = (f"mode={mode} | window={date_filter} | "
               f"campaigns={len(campaigns)} | opens_inserted={total_processed}")
        print(msg)
        write_audit(conn, 200, msg)
        return {"statusCode": 200, "body": msg}

    except Exception as e:
        err = f"Fatal Error: {str(e)}"
        print(err)
        if conn:
            write_audit(conn, 500, err)
        return {"statusCode": 500, "body": err}

    finally:
        if conn:
            conn.close()
