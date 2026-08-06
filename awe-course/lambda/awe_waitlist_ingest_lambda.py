"""
AWE Course — Waitlist ingest Lambda
===================================

Pulls the AWE waitlist from the Campaign Monitor "NSR" list and full-refreshes
`superage.awe_waitlist` in RDS (TRUNCATE + bulk INSERT).

Data flow
---------
    Campaign Monitor API  ──►  this Lambda  ──►  superage.awe_waitlist (RDS)

Then `awe_metrics_lambda` reads that table (plus campaign clicks + subscriber_quiz)
to build the dashboard JSON.

Environment variables
----------------------
Campaign Monitor (plain env vars — no Secrets Manager, per request):
    CM_API_KEY      (required)  Campaign Monitor API key (used as Basic-auth username)
    CM_CLIENT_ID    (optional)  CM client id — informational / logging only
    CM_LIST_ID      (required)  CM list id for the "NSR" waitlist list
    CM_STATES       (optional)  comma list of subscriber states to pull
                                default: "active,unsubscribed"
    CM_PAGE_SIZE    (optional)  page size, default 1000
    CM_API_BASE     (optional)  default https://api.createsend.com/api/v3.3

Database (Secrets Manager, same pattern as the other dashboard lambdas):
    DB_SECRET_ARN   (required)  secret with host/port/dbname/username/password
    DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_SSLMODE  (optional overrides)
    SA_SCHEMA       (optional)  default "superage"

Ops:
    SNS_TOPIC_ARN   (optional)  publish a message here on failure
    AWS_REGION      (optional)  default us-west-1
    DRY_RUN         (optional)  "true" -> fetch + log only, do not write to DB

Dependencies: boto3, psycopg2  (urllib from stdlib for the CM API — no `requests`).
"""

import base64
import json
import logging
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SA_SCHEMA    = os.environ.get("SA_SCHEMA", "superage")
CM_API_BASE  = os.environ.get("CM_API_BASE", "https://api.createsend.com/api/v3.3").rstrip("/")
CM_PAGE_SIZE = int(os.environ.get("CM_PAGE_SIZE", "1000"))
CM_STATES    = [s.strip().lower() for s in os.environ.get("CM_STATES", "active,unsubscribed").split(",") if s.strip()]
# CM's list endpoints filter by "changed since" date; use a far-past date so we
# get every subscriber regardless of when they joined/changed state.
CM_SINCE     = os.environ.get("CM_SINCE", "2000-01-01")
DRY_RUN      = os.environ.get("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}

# CM custom-field keys we promote to their own columns. Everything CM returns is
# also stored whole in the custom_fields JSONB column.
CUSTOM_FIELD_COLUMNS = [
    "sub_level", "oid", "hashed_email", "source",
    "utm_source", "utm_medium", "utm_campaign", "o_event",
]

_db_secret_cache = None


# ─────────────────────────────────────────────────────────────
# Ops: SNS failure alert
# ─────────────────────────────────────────────────────────────

def _alert_failure(context, err):
    arn = os.environ.get("SNS_TOPIC_ARN")
    if not arn:
        logger.warning("SNS_TOPIC_ARN not set — skipping failure alert.")
        return
    try:
        fn = getattr(context, "function_name", "awe_waitlist_ingest")
        boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-west-1")).publish(
            TopicArn=arn,
            Subject=f"[AWE] Waitlist ingest FAILED: {fn}"[:99],
            Message=f"Lambda: {fn}\nError: {err}\n",
        )
        logger.info("Failure alert published to SNS.")
    except Exception as e:  # never let alerting mask the real error
        logger.error("Failed to publish SNS alert: %s", e)


# ─────────────────────────────────────────────────────────────
# DB connection (Secrets Manager, matches sales/campaigns lambdas)
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
# Campaign Monitor API
# ─────────────────────────────────────────────────────────────

def _cm_auth_header():
    key = os.environ["CM_API_KEY"]
    token = base64.b64encode(f"{key}:x".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _cm_get(path, params):
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{CM_API_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": _cm_auth_header(),
        "Content-Type":  "application/json",
        "User-Agent":    "awe-waitlist-ingest/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_state(list_id, state):
    """Fetch all subscribers of one CM state (active/unsubscribed/...), paginated.

    Uses the LIST endpoint: GET /lists/{listid}/{state}.json
    (not /subscribers/... — that path is for single-subscriber lookups).
    """
    rows, page, pages = [], 1, 1
    while page <= pages:
        try:
            data = _cm_get(f"/lists/{list_id}/{state}.json", {
                "date": CM_SINCE,
                "page": page,
                "pagesize": CM_PAGE_SIZE,
                "orderfield": "email",
                "orderdirection": "asc",
            })
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"CM API {state} page {page} -> HTTP {e.code}: {body[:300]}")
        results = data.get("Results", []) or []
        for r in results:
            r["_cm_state"] = state
        rows.extend(results)
        pages = int(data.get("NumberOfPages", 1) or 1)
        logger.info("CM %s: page %d/%d (+%d rows)", state, page, pages, len(results))
        page += 1
    return rows


# ─────────────────────────────────────────────────────────────
# Transform
# ─────────────────────────────────────────────────────────────

def _parse_dt(val):
    if not val:
        return None
    val = str(val).strip()
    if not val or val.startswith("0000"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(val[:19], fmt)
        except ValueError:
            continue
    return None


def _custom_fields_map(sub):
    out = {}
    for cf in sub.get("CustomFields", []) or []:
        k = (cf.get("Key") or "").strip()
        # Campaign Monitor returns the field Key wrapped in brackets (e.g. "[oid]").
        # Strip the surrounding [ ] so we map to plain keys (oid, source, ...).
        if k.startswith("[") and k.endswith("]"):
            k = k[1:-1].strip()
        if not k:
            continue
        # CM can return repeated keys; last write wins (single-value fields here).
        out[k] = cf.get("Value")
    return out


def transform(sub):
    """CM subscriber dict -> row tuple for superage.awe_waitlist."""
    email = (sub.get("EmailAddress") or "").strip().lower()
    if not email:
        return None
    state = sub.get("State") or sub.get("_cm_state", "").title()
    cm_state = sub.get("_cm_state", "").lower()

    # Campaign Monitor exposes TWO distinct dates on each subscriber:
    #   ListJoinedDate — when they first JOINED the list  ("Joined via API on ...")
    #   Date           — the last state change ("Active since ..." / unsubscribe date)
    # These differ (e.g. joined 22 Jul, active 23 Jul), so we keep them separate.
    joined = _parse_dt(sub.get("ListJoinedDate"))
    changed = _parse_dt(sub.get("Date"))

    date_joined = joined
    if cm_state == "active":
        date_subscribed = changed or joined
        date_unsubscribed = None
    elif cm_state == "unsubscribed":
        date_subscribed = joined            # original join = best subscribe signal
        date_unsubscribed = changed         # Date = the unsubscribe timestamp
    else:  # bounced / other
        date_subscribed = changed or joined
        date_unsubscribed = None

    cf = _custom_fields_map(sub)
    col_vals = {c: cf.get(c) for c in CUSTOM_FIELD_COLUMNS}

    return (
        email,
        sub.get("Name"),
        date_joined,
        date_subscribed,
        date_unsubscribed,
        state,
        col_vals["sub_level"],
        col_vals["oid"],
        col_vals["hashed_email"],
        col_vals["source"],
        col_vals["utm_source"],
        col_vals["utm_medium"],
        col_vals["utm_campaign"],
        col_vals["o_event"],
        json.dumps(cf, default=str),
    )


INSERT_COLS = (
    "email, name, date_joined, date_subscribed, date_unsubscribed, state, "
    "sub_level, oid, hashed_email, source, utm_source, utm_medium, "
    "utm_campaign, o_event, custom_fields"
)


# ─────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    try:
        return _run(event, context)
    except Exception as err:
        logger.exception("AWE waitlist ingest failed")
        _alert_failure(context, err)
        raise


def _run(event, context):
    list_id = os.environ["CM_LIST_ID"]
    client_id = os.environ.get("CM_CLIENT_ID", "")
    logger.info("AWE waitlist ingest — list=%s client=%s states=%s dry_run=%s",
                list_id, client_id, CM_STATES, DRY_RUN)

    # 1) Fetch from Campaign Monitor
    raw = []
    for state in CM_STATES:
        raw.extend(fetch_state(list_id, state))
    logger.info("Fetched %d CM subscribers across states %s", len(raw), CM_STATES)

    # 2) Transform (dedupe by email; prefer active over unsubscribed if both seen)
    by_email = {}
    state_rank = {"active": 3, "unsubscribed": 2, "bounced": 1}
    for sub in raw:
        row = transform(sub)
        if not row:
            continue
        email = row[0]
        rank = state_rank.get(sub.get("_cm_state", "").lower(), 0)
        prev = by_email.get(email)
        if prev is None or rank >= prev[0]:
            by_email[email] = (rank, row)
    rows = [v[1] for v in by_email.values()]
    logger.info("Prepared %d unique waitlist rows", len(rows))

    if DRY_RUN:
        logger.info("DRY_RUN=true — not writing to DB. Sample: %s",
                    rows[0] if rows else "none")
        return {"statusCode": 200, "fetched": len(raw), "unique": len(rows), "written": 0,
                "dry_run": True}

    # 3) Full refresh: TRUNCATE + bulk insert inside one transaction
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f'TRUNCATE TABLE {SA_SCHEMA}.awe_waitlist')
                if rows:
                    psycopg2.extras.execute_values(
                        cur,
                        f"INSERT INTO {SA_SCHEMA}.awe_waitlist ({INSERT_COLS}) VALUES %s",
                        rows,
                        template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                        page_size=500,
                    )
        logger.info("Wrote %d rows to %s.awe_waitlist", len(rows), SA_SCHEMA)
    finally:
        conn.close()

    return {"statusCode": 200, "fetched": len(raw), "unique": len(rows), "written": len(rows)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(lambda_handler({}, None), default=str))
