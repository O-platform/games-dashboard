"""
lambda_clicks_incremental.py
-----------------------------
AWS Lambda — incremental sync of articles_clicks + subscriber_clicks.
Scheduled daily via EventBridge. Reads from superage."Campaigns_Clicks",
writes to superage.articles_clicks and superage.subscriber_clicks.

Checkpoint: reads last successful window_end from clicks_audit,
            processes window_end+1 → yesterday.

ENV VARS:
  DB_HOST       RDS endpoint
  DB_PORT       optional, default 5432
  DB_NAME       database name
  DB_USER       db username
  DB_PASSWORD   db password
  DB_SECRET_ARN optional — AWS Secrets Manager ARN (overrides above)
  AWS_REGION    optional, default us-west-1
  REBUILD_BATCH_SIZE     optional, default 300 — rows re-scanned per
                         `mode: all` chunk (see below). Kept small because
                         each row costs a full scan/join against the raw
                         Campaigns_Clicks table, unlike the cheap per-row
                         cost of the normal incremental path.
  REBUILD_TIME_SAFETY_MS optional, default 45000 — stop processing and
                         self-invoke for the next chunk once less than
                         this much execution time remains.

Lambda event examples:
  {}                                    — normal incremental
  {"start":"2026-05-01","end":"2026-05-07"}  — explicit window override
  {"skip_airtable":true}               — skip Airtable enrichment
  {"mode":"all"}                       — ONE-TIME FULL REBUILD (see below).
                                          Re-derives unique_clicks AND
                                          non_unique_clicks for every row
                                          in articles_clicks straight from
                                          Campaigns_Clicks, in bounded
                                          batches. Self-invokes (async, via
                                          boto3) for the next batch before
                                          the Lambda would time out, using
                                          a resumable cursor persisted in
                                          clicks_audit (mode='rebuild_all'),
                                          so it can safely run across many
                                          chained invocations without you
                                          re-triggering it manually. Safe
                                          to re-run/resume at any time —
                                          it never re-processes an id it
                                          has already committed past.


═══════════════════════════════════════════════════════════════════════
BUG FIX (this revision) — non_unique_clicks fragmentation
═══════════════════════════════════════════════════════════════════════
Previously:
  - unique_clicks     was FULLY RECOMPUTED every run from Campaigns_Clicks,
                       matched only on (issue_name, normalized url) — it
                       never looked at article_title.
  - non_unique_clicks was INCREMENTALLY ACCUMULATED ("existing + this
                       batch's count"), keyed on the upsert conflict
                       target (issue_name, url, article_title).

If article_title_for() ever resolved a even slightly different title for
the SAME logical article across two runs (whitespace, casing, an Airtable
re-match, etc.), the ON CONFLICT stopped matching the existing row. A NEW
row got inserted, non_unique_clicks restarted from zero for that fragment,
and the old row's accumulated total was stranded under a title spelling no
future run would ever hit again. unique_clicks kept looking correct on
every fragment (it always recomputes the true total, blind to title),
which is exactly why unique_clicks > non_unique_clicks showed up on ~15%
of rows — non_unique_clicks was a stranded partial sum, not really wrong
data, just permanently incomplete.

Fix: non_unique_clicks is now recomputed from source in the SAME step-2
query as unique_clicks (one extra COUNT(*), one extra SET) — no more
incremental accumulation, no more dependency on (issue_name, url,
article_title) staying stable across runs. Any article touched by a
future run self-heals to its true total immediately. Rows NOT touched by
any future run (dormant old articles) stay wrong forever under this
change alone — that's what `mode: all` is for; it forces every existing
row to be touched once.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

from sa_classify import (
    SUPPORTED_TYPES,
    classify_url,
    article_title_for,
    normalize_url_key,
    parse_int,
    parse_date_value,
    parse_issue_date_from_name,
    compute_position_category,
)

try:
    import boto3
except ImportError:
    boto3 = None

# ═══════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("lambda_clicks_incremental")

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════

ARTICLES_TABLE       = os.environ.get("ARTICLES_TABLE",       "superage.articles_clicks")
SUBSCRIBER_TABLE     = os.environ.get("SUBSCRIBER_TABLE",     "superage.subscriber_clicks")
AUDIT_TABLE          = os.environ.get("AUDIT_TABLE",          "superage.clicks_audit")
AIRTABLE_TABLE       = os.environ.get("AIRTABLE_TABLE",       "superage.sa_airtable_articles")
AIRTABLE_SALES_TABLE = os.environ.get("AIRTABLE_SALES_TABLE", "superage.sa_airtable_sales")
SOURCE_TABLE         = os.environ.get("SOURCE_TABLE",         'superage."Campaigns_Clicks"')
BATCH_SIZE           = int(os.environ.get("BATCH_SIZE",       "50000"))
HIGH_PCT             = float(os.environ.get("HIGH_PCT",       "0.30"))
MEDIUM_PCT           = float(os.environ.get("MEDIUM_PCT",     "0.70"))

# Full-rebuild (`mode: all`) tuning — see module docstring.
REBUILD_BATCH_SIZE      = int(os.environ.get("REBUILD_BATCH_SIZE", "300"))
REBUILD_TIME_SAFETY_MS  = int(os.environ.get("REBUILD_TIME_SAFETY_MS", "45000"))

# ═══════════════════════════════════════════════════════════════
# DB connection
# ═══════════════════════════════════════════════════════════════

def get_db_creds() -> dict:
    secret_arn = os.environ.get("DB_SECRET_ARN", "").strip()
    if secret_arn:
        if boto3 is None:
            raise RuntimeError("boto3 required for DB_SECRET_ARN")
        region = os.environ.get("AWS_REGION", "us-west-1")
        resp   = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_arn)
        s      = json.loads(resp["SecretString"])
        return {
            "host":     s.get("host"),
            "port":     str(s.get("port", 5432)),
            "dbname":   s.get("dbname", s.get("database", "postgres")),
            "username": s.get("username", s.get("user")),
            "password": s.get("password"),
        }
    return {
        "host":     os.environ["DB_HOST"],
        "port":     os.environ.get("DB_PORT", "5432"),
        "dbname":   os.environ["DB_NAME"],
        "username": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def get_conn():
    c = get_db_creds()
    return psycopg2.connect(
        host=c["host"], port=int(c.get("port", 5432)),
        dbname=c["dbname"], user=c.get("username", c.get("user")),
        password=c["password"],
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=20,
    )

# ═══════════════════════════════════════════════════════════════
# Window detection
# ═══════════════════════════════════════════════════════════════

def get_window(conn, event: dict) -> Tuple[datetime, datetime, bool]:
    """
    window_start = MAX(run_at) of last successful run — exact timestamp,
                   no gap, no duplicates.
    window_end   = NOW() at start of this invocation.
    Event can override with explicit start/end ISO timestamps.
    """
    window_end = datetime.now(timezone.utc)

    if event.get("start"):
        window_start = datetime.fromisoformat(str(event["start"]))
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)
        if event.get("end"):
            window_end = datetime.fromisoformat(str(event["end"]))
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
        return window_start, window_end, False

    su, tu = AUDIT_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT MAX(run_at) FROM {su}."{tu}"
            WHERE status = 'success'
              AND mode <> 'rebuild_all'
        """)
        row = cur.fetchone()

    last_run_at = row[0] if row and row[0] else None

    if last_run_at:
        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=timezone.utc)
        window_start = last_run_at
    else:
        LOG.warning("No prior successful run — defaulting to last 7 days.")
        window_start = window_end - timedelta(days=7)

    if window_start >= window_end:
        return window_start, window_end, True

    LOG.info("Incremental window: %s → %s", window_start, window_end)
    return window_start, window_end, False

# ═══════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════

def open_audit(conn, started_at: datetime,
               window_start: datetime, window_end: datetime,
               mode: str = "incremental") -> int:
    su, tu = AUDIT_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {su}."{tu}"
                (run_at, runner, mode, status, window_start, window_end)
            VALUES (%s,'lambda',%s,'running',%s,%s) RETURNING id
        """, (started_at, mode, window_start, window_end))
        audit_id = cur.fetchone()[0]
    conn.commit()
    return audit_id


def close_audit(conn, audit_id: int, started_at: datetime, status: str,
                source_rows: int, kept_rows: int,
                article_rows: int, subscriber_rows: int,
                story_n: int, category_n: int,
                type_breakdown: dict, error: Optional[str] = None):
    finished_at = datetime.now(timezone.utc)
    duration    = (finished_at - started_at).total_seconds()
    su, tu      = AUDIT_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {su}."{tu}" SET
                status                   = %s,
                source_rows              = %s,
                kept_rows                = %s,
                excluded_rows            = %s,
                article_rows_upserted    = %s,
                subscriber_rows_upserted = %s,
                story_positions_updated  = %s,
                position_categories_set  = %s,
                type_breakdown           = %s,
                duration_seconds         = %s,
                error_message            = %s,
                finished_at              = %s
            WHERE id = %s
        """, (status, source_rows, kept_rows, source_rows - kept_rows,
              article_rows, subscriber_rows, story_n, category_n,
              json.dumps(type_breakdown), round(duration, 2),
              error, finished_at, audit_id))
    conn.commit()
    LOG.info("Audit — status=%s source=%d kept=%d art=%d sub=%d dur=%.1fs",
             status, source_rows, kept_rows, article_rows, subscriber_rows, duration)

# ═══════════════════════════════════════════════════════════════
# Source iteration
# ═══════════════════════════════════════════════════════════════

def iter_clicks(conn, window_start: datetime, window_end: datetime):
    """
    Filter by "Date" using full timestamps.
    "Date" in Campaigns_Clicks is naive Mountain Time (America/Denver).
    window_start/end are UTC — convert to MT naive before comparing
    so clicks like 02:51 MT are not missed when window_start is 05:00 UTC.
    """
    from zoneinfo import ZoneInfo
    MT    = ZoneInfo("America/Denver")
    ws_mt = window_start.astimezone(MT).replace(tzinfo=None)
    we_mt = window_end.astimezone(MT).replace(tzinfo=None)

    query = f"""
        SELECT
            LOWER(TRIM("EmailAddress "))   AS email,
            TRIM("URL")                    AS url,
            COALESCE(TRIM(issue_name), '') AS issue_name,
            issue_date::date               AS issue_date
        FROM {SOURCE_TABLE}
        WHERE "URL" IS NOT NULL
          AND TRIM("URL") <> ''
          AND "Date" >= %s
          AND "Date" <  %s
        ORDER BY "Date" ASC NULLS LAST
    """
    with conn.cursor("lambda_clicks_cursor") as cur:
        cur.itersize = BATCH_SIZE
        cur.execute(query, (ws_mt, we_mt))
        while True:
            rows = cur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            yield rows

# ═══════════════════════════════════════════════════════════════
# Aggregation
# ═══════════════════════════════════════════════════════════════

def aggregate_batch(rows, art_agg: dict, sub_agg: dict) -> Tuple[int, int]:
    kept = excluded = 0
    for email, raw_url, issue_name, issue_date in rows:
        click_type, clean_url, label = classify_url(raw_url, issue_name)
        if click_type not in SUPPORTED_TYPES:
            excluded += 1
            continue

        article_title = article_title_for(click_type, clean_url, label)
        issue_key     = issue_name if issue_name else "Unknown Issue"
        art_key       = (issue_key, clean_url, article_title)

        if art_key not in art_agg:
            art_agg[art_key] = {
                "type":          click_type,
                "is_sponsor":    1 if click_type == "sponsor" else None,
                "issue_date":    issue_date,
                "unique_emails": set(),
                "total":         0,
            }
        art_agg[art_key]["total"] += 1
        if email:
            art_agg[art_key]["unique_emails"].add(email)

        if email:
            if email not in sub_agg:
                sub_agg[email] = {
                    "unique_urls": set(),
                    "total":       0,
                }
            sub_agg[email]["total"] += 1
            sub_agg[email]["unique_urls"].add(clean_url)

        kept += 1
    return kept, excluded

# ═══════════════════════════════════════════════════════════════
# Upserts
# ═══════════════════════════════════════════════════════════════

def upsert_articles(conn, art_agg: dict) -> int:
    """
    BUG FIX: non_unique_clicks used to be accumulated incrementally
    ("existing + this batch's count") keyed on the ON CONFLICT target
    (issue_name, url, article_title). If article_title ever resolved
    differently across runs for the same logical article (a whitespace/
    casing difference, a re-match against updated Airtable data, etc.),
    the conflict target silently missed and a NEW fragment row was
    created — stranding the old row's accumulated total forever under a
    title spelling no future run would hit again.

    Fix: non_unique_clicks is now recomputed from source in the SAME
    step-2 query as unique_clicks — both match ONLY on
    (issue_name, normalized url), never on article_title, so neither
    metric can fragment across title drift anymore. Any article touched
    by this run gets its TRUE, COMPLETE total for both columns,
    unconditionally — no dependency on incremental bookkeeping surviving
    across runs.
    """
    if not art_agg:
        return 0

    sa, ta = ARTICLES_TABLE.split(".", 1)

    # ── step 1: insert new rows / refresh metadata on existing ones.
    #    non_unique_clicks is given a placeholder value here (this
    #    batch's own count) purely so a brand-new row has SOMETHING
    #    non-null before step 2 unconditionally overwrites it with the
    #    true recomputed total a few lines down.
    rows = [
        (issue_name, clean_url, title,
         v["type"], v["is_sponsor"], v["issue_date"],
         v["total"])
        for (issue_name, clean_url, title), v in art_agg.items()
    ]
    with conn.cursor() as cur:
        execute_values(cur, f"""
            INSERT INTO {sa}."{ta}"
                (issue_name, url, article_title, type, is_sponsor,
                 issue_date, non_unique_clicks)
            VALUES %s
            ON CONFLICT (issue_name, url, article_title) DO UPDATE SET
                type              = EXCLUDED.type,
                is_sponsor        = EXCLUDED.is_sponsor,
                issue_date        = COALESCE(EXCLUDED.issue_date, "{ta}".issue_date),
                updated_at        = NOW()
        """, rows, page_size=1000)
    conn.commit()

    # ── step 2: recompute BOTH unique_clicks and non_unique_clicks from
    #    source, matched only on (issue_name, normalized url) — never on
    #    article_title. Self-healing: whatever fragment row this
    #    particular (issue_name, url, article_title) key landed on gets
    #    the article's TRUE total, same as every other fragment sharing
    #    that (issue_name, url) would if touched.
    affected = list({(v["issue_date"], clean_url, issue_name, title)
                     for (issue_name, clean_url, title), v in art_agg.items()})

    with conn.cursor() as cur:
        for issue_date, url, issue_name, title in affected:
            cur.execute(f"""
                UPDATE {sa}."{ta}" ac
                SET
                    unique_clicks = src.unique_clicks,
                    non_unique_clicks = src.non_unique_clicks,
                    updated_at = NOW()
                FROM (
                    SELECT
                        COUNT(DISTINCT LOWER(TRIM("EmailAddress "))) AS unique_clicks,
                        COUNT(*)                                    AS non_unique_clicks
                    FROM {SOURCE_TABLE}
                    WHERE COALESCE(TRIM(issue_name), '') = %s
                      AND RTRIM(SPLIT_PART(TRIM("URL"), '?', 1), '/') = RTRIM(SPLIT_PART(%s, '?', 1), '/')
                      AND "EmailAddress " IS NOT NULL
                ) src
                WHERE ac.issue_name    = %s
                  AND ac.url           = %s
                  AND ac.article_title = %s
            """, (issue_name, url, issue_name, url, title))
    conn.commit()

    LOG.info("Upserted %d article rows (unique + non_unique both recomputed from source)", len(rows))
    return len(rows)


def upsert_subscribers(conn, sub_agg: dict) -> int:
    """
    non_unique_clicks: add on top (incremental accumulation).
    unique_clicks (distinct URLs all-time): recalculate from scratch
    from Campaigns_Clicks for each affected email so it's always accurate.

    NOTE: subscriber_clicks is keyed on email_address alone (not on any
    per-article title-like field), so it does NOT share articles_clicks'
    fragmentation bug — an email's identity can't drift the way an
    article's derived title can. Left as incremental accumulation.
    """
    if not sub_agg:
        return 0

    ss, ts = SUBSCRIBER_TABLE.split(".", 1)

    # ── step 1: upsert non_unique (add) ───────────────────────────
    rows = [
        (email, 0, v["total"])   # unique placeholder 0, recalc below
        for email, v in sub_agg.items()
    ]
    with conn.cursor() as cur:
        execute_values(cur, f"""
            INSERT INTO {ss}."{ts}"
                (email_address, unique_clicks, non_unique_clicks)
            VALUES %s
            ON CONFLICT (email_address) DO UPDATE SET
                non_unique_clicks = "{ts}".non_unique_clicks + EXCLUDED.non_unique_clicks,
                updated_at        = NOW()
        """, rows, page_size=1000)
    conn.commit()

    # ── step 2: recalculate unique_clicks (distinct stripped URLs) ─
    affected_emails = list(sub_agg.keys())
    with conn.cursor() as cur:
        execute_values(cur, f"""
            UPDATE {ss}."{ts}" sc
            SET unique_clicks = sub.unique_count,
                updated_at    = NOW()
            FROM (
                SELECT
                    LOWER(TRIM("EmailAddress ")) AS email,
                    COUNT(DISTINCT
                        RTRIM(REGEXP_REPLACE(
                            LOWER(SPLIT_PART(SPLIT_PART(TRIM("URL"),'?',1),'#',1)),
                        '^https?://(www\\.)?',''),'/')
                    ) AS unique_count
                FROM {SOURCE_TABLE}
                WHERE "EmailAddress " IS NOT NULL
                  AND TRIM("EmailAddress ") <> ''
                  AND LOWER(TRIM("EmailAddress ")) IN %s
                  AND "URL" IS NOT NULL
                GROUP BY LOWER(TRIM("EmailAddress "))
            ) sub
            WHERE sc.email_address = sub.email
        """, [tuple(affected_emails)], page_size=1000)
    conn.commit()

    LOG.info("Upserted %d subscriber rows (unique recalculated)", len(rows))
    return len(rows)

# ═══════════════════════════════════════════════════════════════
# Full rebuild (`mode: all`) — one-time historical correction
# ═══════════════════════════════════════════════════════════════
#
# The step-2 fix above only self-heals articles touched by a FUTURE
# incremental run. Any article that never receives another click (an
# old, dormant newsletter issue) would keep its stale, fragmented
# non_unique_clicks forever. This full rebuild forces every EXISTING row
# in articles_clicks to be touched once, recomputing unique_clicks AND
# non_unique_clicks straight from Campaigns_Clicks — the same query
# upsert_articles' step 2 already runs, just applied by primary key
# across the whole table instead of only the rows a batch of new clicks
# happened to touch.
#
# Re-scanning the whole table in one Lambda invocation will time out, so
# this processes REBUILD_BATCH_SIZE rows at a time (ordered by id), and
# — before running low on remaining execution time — asynchronously
# self-invokes (via boto3) with {"mode": "all"} so the next batch
# continues automatically. Progress is a simple resumable cursor: the
# MAX(id) already corrected, persisted in clicks_audit (mode='rebuild_all').
# Safe to resume from any point; never re-processes an id it has already
# committed past. Safe to kick off again later too — it just starts a
# fresh pass from id > 0.
#
# NOT run during a normal `mode: all` pass: Airtable/sponsor enrichment.
# Those are independent of this bug and can be refreshed via a normal
# incremental run afterwards if needed.

def _ensure_rebuild_cursor_column(conn):
    su, tu = AUDIT_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            ALTER TABLE {su}."{tu}"
            ADD COLUMN IF NOT EXISTS rebuild_last_id BIGINT
        """)
    conn.commit()


def _get_rebuild_cursor(conn) -> int:
    su, tu = AUDIT_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT MAX(rebuild_last_id) FROM {su}."{tu}"
            WHERE mode = 'rebuild_all'
        """)
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0


def _fetch_rebuild_batch(conn, last_id: int, limit: int):
    """Rows ordered by id > last_id, oldest-first, bounded to `limit`."""
    sa, ta = ARTICLES_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, issue_name, url, article_title
            FROM {sa}."{ta}"
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """, (last_id, limit))
        return cur.fetchall()


def _recompute_rebuild_batch(conn, batch) -> int:
    """Same recompute as upsert_articles' step 2, applied by primary key."""
    sa, ta = ARTICLES_TABLE.split(".", 1)
    n = 0
    with conn.cursor() as cur:
        for row_id, issue_name, url, _title in batch:
            cur.execute(f"""
                UPDATE {sa}."{ta}" ac
                SET
                    unique_clicks = src.unique_clicks,
                    non_unique_clicks = src.non_unique_clicks,
                    updated_at = NOW()
                FROM (
                    SELECT
                        COUNT(DISTINCT LOWER(TRIM("EmailAddress "))) AS unique_clicks,
                        COUNT(*)                                    AS non_unique_clicks
                    FROM {SOURCE_TABLE}
                    WHERE COALESCE(TRIM(issue_name), '') = %s
                      AND RTRIM(SPLIT_PART(TRIM("URL"), '?', 1), '/') = RTRIM(SPLIT_PART(%s, '?', 1), '/')
                      AND "EmailAddress " IS NOT NULL
                ) src
                WHERE ac.id = %s
            """, (issue_name, url, row_id))
            n += 1
    conn.commit()
    return n


def _self_invoke_rebuild(context) -> bool:
    """Fires an async self-invocation with {"mode": "all"} so the next
    batch continues without blocking this invocation. Returns True if the
    self-invoke was sent, False if it couldn't be (e.g. no boto3, or not
    actually running in Lambda / no context)."""
    if boto3 is None:
        LOG.warning("boto3 unavailable — cannot self-invoke for next rebuild batch. "
                    "Re-invoke manually with {\"mode\": \"all\"} to continue.")
        return False
    function_name = getattr(context, "function_name", None) or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        LOG.warning("No function_name available — cannot self-invoke. "
                    "Re-invoke manually with {\"mode\": \"all\"} to continue.")
        return False
    try:
        region = os.environ.get("AWS_REGION", "us-west-1")
        boto3.client("lambda", region_name=region).invoke(
            FunctionName=function_name,
            InvocationType="Event",  # async — don't wait, don't hold this invocation open
            Payload=json.dumps({"mode": "all"}).encode("utf-8"),
        )
        LOG.info("Self-invoked %s for next rebuild batch.", function_name)
        return True
    except Exception as exc:
        LOG.exception("Self-invoke failed: %s — re-invoke manually with {\"mode\": \"all\"} to continue.", exc)
        return False


def run_full_rebuild(conn, context) -> dict:
    started_at = datetime.now(timezone.utc)
    _ensure_rebuild_cursor_column(conn)

    last_id      = _get_rebuild_cursor(conn)
    audit_id     = open_audit(conn, started_at, started_at, started_at, mode="rebuild_all")
    total_rows   = 0
    batches      = 0
    reached_end  = False

    try:
        while True:
            remaining_ms = (
                context.get_remaining_time_in_millis()
                if context and hasattr(context, "get_remaining_time_in_millis")
                else REBUILD_TIME_SAFETY_MS + 1  # no context (local test) — just run one batch
            )
            if remaining_ms < REBUILD_TIME_SAFETY_MS:
                LOG.info("Approaching time limit (%dms remaining) — stopping to self-invoke.", remaining_ms)
                break

            batch = _fetch_rebuild_batch(conn, last_id, REBUILD_BATCH_SIZE)
            if not batch:
                reached_end = True
                break

            n = _recompute_rebuild_batch(conn, batch)
            last_id = batch[-1][0]
            total_rows += n
            batches += 1

            # Persist progress after EVERY batch, not just at the end, so a
            # timeout mid-run never loses committed work.
            su, tu = AUDIT_TABLE.split(".", 1)
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE {su}."{tu}" SET rebuild_last_id = %s WHERE id = %s
                """, (last_id, audit_id))
            conn.commit()

            LOG.info("Rebuild batch=%d rows=%d cursor(last_id)=%s total=%d",
                      batches, n, last_id, total_rows)

        if reached_end:
            close_audit(conn, audit_id, started_at, "success",
                        total_rows, total_rows, total_rows, 0, 0, 0, {})
            LOG.info("Full rebuild COMPLETE — no more rows after id=%s. Total rows this pass: %d",
                      last_id, total_rows)
            return {"status": "complete", "total_rows_this_pass": total_rows, "last_id": last_id}

        # More work remains — close this invocation's audit row as a
        # successful partial pass, then self-invoke for the next chunk.
        close_audit(conn, audit_id, started_at, "success",
                    total_rows, total_rows, total_rows, 0, 0, 0, {})
        invoked = _self_invoke_rebuild(context)
        return {
            "status": "continuing" if invoked else "paused_manual_resume_needed",
            "rows_this_invocation": total_rows,
            "last_id": last_id,
        }

    except Exception as exc:
        LOG.exception("Rebuild batch failed: %s", exc)
        close_audit(conn, audit_id, started_at, "failed",
                    total_rows, total_rows, total_rows, 0, 0, 0, {}, str(exc))
        raise

# ═══════════════════════════════════════════════════════════════
# Airtable enrichment (affected issues only)
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ArticleRow:
    issue_name: str; url: str; article_title: str; type: str
    story_position: Optional[int]; position_category: Optional[str]
    issue_date: Optional[date]; url_key: str

@dataclass(frozen=True)
class AirtableRow:
    canonical_url: str; story_position: int
    issue_date: Optional[date]; url_key: str

@dataclass(frozen=True)
class MatchedRow:
    issue_name: str; url: str; article_title: str
    new_story_position: int; issue_date: Optional[date]


def load_articles_for_enrichment(conn,
                                  affected_issues: Optional[List[str]] = None
                                  ) -> List[ArticleRow]:
    sa, ta = ARTICLES_TABLE.split(".", 1)
    where  = "WHERE url IS NOT NULL AND issue_name IS NOT NULL AND article_title IS NOT NULL"
    params: list = []
    if affected_issues:
        where += " AND issue_name = ANY(%s)"
        params.append(affected_issues)
    rows = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT issue_name, url, article_title,
                   LOWER(COALESCE(type,'')) AS type,
                   story_position, position_category, issue_date
            FROM {sa}."{ta}" {where}
        """, params if params else None)
        for r in cur.fetchall():
            iname = str(r["issue_name"])
            idate = parse_date_value(r.get("issue_date")) or parse_issue_date_from_name(iname)
            url   = str(r["url"] or "")
            key   = normalize_url_key(url)
            if not key: continue
            rows.append(ArticleRow(
                issue_name=iname, url=url,
                article_title=str(r["article_title"]),
                type=str(r["type"]),
                story_position=parse_int(r["story_position"]),
                position_category=str(r["position_category"]) if r["position_category"] is not None else None,
                issue_date=idate, url_key=key,
            ))
    LOG.info("Loaded %d articles for enrichment", len(rows))
    return rows


def load_airtable_rows(conn) -> List[AirtableRow]:
    sat, tat = AIRTABLE_TABLE.split(".", 1)
    rows = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT canonical_url, story_position, issue_date
            FROM {sat}."{tat}"
            WHERE canonical_url IS NOT NULL
              AND TRIM(canonical_url::text) <> ''
              AND story_position IS NOT NULL
        """)
        for r in cur.fetchall():
            pos = parse_int(r["story_position"])
            if pos is None: continue
            url = str(r["canonical_url"] or "")
            key = normalize_url_key(url)
            if not key: continue
            rows.append(AirtableRow(
                canonical_url=url, story_position=pos,
                issue_date=parse_date_value(r.get("issue_date")),
                url_key=key,
            ))
    LOG.info("Loaded %d Airtable rows", len(rows))
    return rows


def run_airtable_enrichment(conn,
                             affected_issues: Optional[List[str]]) -> Tuple[int, int]:
    LOG.info("─── Airtable enrichment ───")
    articles      = load_articles_for_enrichment(conn, affected_issues)
    airtable_rows = load_airtable_rows(conn)
    if not airtable_rows:
        LOG.warning("No Airtable rows — skipping.")
        return 0, 0

    by_key_date: Dict[Tuple[str, date], List[AirtableRow]] = defaultdict(list)
    by_key:      Dict[str, List[AirtableRow]]              = defaultdict(list)
    for r in airtable_rows:
        by_key[r.url_key].append(r)
        if r.issue_date:
            by_key_date[(r.url_key, r.issue_date)].append(r)

    matched = []
    for a in articles:
        air = None
        if a.issue_date:
            exact = by_key_date.get((a.url_key, a.issue_date), [])
            if len(exact) == 1: air = exact[0]
            elif len(exact) > 1 and len({x.story_position for x in exact}) == 1: air = exact[0]
        if air is None:
            candidates = by_key.get(a.url_key, [])
            if len(candidates) == 1: air = candidates[0]
        if air:
            matched.append(MatchedRow(
                issue_name=a.issue_name, url=a.url,
                article_title=a.article_title,
                new_story_position=air.story_position,
                issue_date=a.issue_date,
            ))
    LOG.info("Matched=%d", len(matched))

    sa, ta = ARTICLES_TABLE.split(".", 1)
    story_n = category_n = 0

    if matched:
        pos_vals = [(m.issue_name, m.url, m.article_title, m.new_story_position) for m in matched]
        with conn.cursor() as cur:
            execute_values(cur, f"""
                UPDATE {sa}."{ta}" ac
                SET story_position = d.story_position, updated_at = NOW()
                FROM (VALUES %s) AS d(issue_name, url, article_title, story_position)
                WHERE ac.issue_name = d.issue_name AND ac.url = d.url
                  AND ac.article_title = d.article_title
            """, pos_vals, page_size=500)
            story_n = cur.rowcount
        conn.commit()

    new_pos = {(m.issue_name, m.url, m.article_title): m.new_story_position for m in matched}
    by_issue: Dict[str, list] = defaultdict(list)
    for a in articles:
        pos = new_pos.get((a.issue_name, a.url, a.article_title), a.story_position)
        if pos is None: continue
        by_issue[a.issue_name].append((pos, a.url, a.article_title, a.type))

    cat_vals = []
    for issue_name, items in by_issue.items():
        items_s = sorted(items, key=lambda x: (x[0], x[3], x[2].lower(), x[1].lower()))
        total   = len(items_s)
        for rank, (_p, url, title, _t) in enumerate(items_s, start=1):
            cat_vals.append((issue_name, url, title,
                             compute_position_category(rank, total, HIGH_PCT, MEDIUM_PCT)))

    if cat_vals:
        with conn.cursor() as cur:
            execute_values(cur, f"""
                UPDATE {sa}."{ta}" ac
                SET position_category = d.position_category, updated_at = NOW()
                FROM (VALUES %s) AS d(issue_name, url, article_title, position_category)
                WHERE ac.issue_name = d.issue_name AND ac.url = d.url
                  AND ac.article_title = d.article_title
            """, cat_vals, page_size=500)
            category_n = cur.rowcount
        conn.commit()

    LOG.info("story_positions=%d  position_categories=%d", story_n, category_n)
    return story_n, category_n


# ═══════════════════════════════════════════════════════════════
# Sales sponsor name enrichment
# ═══════════════════════════════════════════════════════════════

def enrich_sponsor_names(conn,
                          affected_issues: Optional[List[str]] = None) -> int:
    """
    Joins articles_clicks against sa_airtable_sales by issue_date + URL.
    Writes sponsor_name (extracted from JSON array) to articles_clicks.
    Applied to ALL rows regardless of type.
    Only processes affected_issues when provided (incremental mode).
    """
    sa, ta = ARTICLES_TABLE.split(".", 1)
    ss, ts = AIRTABLE_SALES_TABLE.split(".", 1)

    issue_filter = ""
    if affected_issues:
        issue_filter = f"AND ac.issue_name = ANY(ARRAY{affected_issues!r})"

    with conn.cursor() as cur:
        cur.execute(f"""
            WITH
            sales AS (
                SELECT
                    issue_date::date                         AS issue_date,
                    RTRIM(REGEXP_REPLACE(
                        LOWER(SPLIT_PART(SPLIT_PART(
                            COALESCE(sponsor_tracking_link,''),'?',1),'#',1)),
                        '^https?://(www\\.)?',''),'/')       AS sponsor_url_key,
                    SPLIT_PART(RTRIM(REGEXP_REPLACE(
                        LOWER(SPLIT_PART(SPLIT_PART(
                            COALESCE(sponsor_tracking_link,''),'?',1),'#',1)),
                        '^https?://(www\\.)?',''),'/'),
                    '/',1)                                   AS sponsor_domain,
                    RTRIM(REGEXP_REPLACE(
                        LOWER(SPLIT_PART(SPLIT_PART(
                            COALESCE(affiliate_tracking_link,''),'?',1),'#',1)),
                        '^https?://(www\\.)?',''),'/')       AS affiliate_url_key,
                    SPLIT_PART(RTRIM(REGEXP_REPLACE(
                        LOWER(SPLIT_PART(SPLIT_PART(
                            COALESCE(affiliate_tracking_link,''),'?',1),'#',1)),
                        '^https?://(www\\.)?',''),'/'),
                    '/',1)                                   AS affiliate_domain,
                    TRIM(BOTH '"' FROM TRIM(BOTH '[]' FROM
                        COALESCE(sponsor_name::text,'')
                    ))                                       AS sponsor_name_text
                FROM {ss}."{ts}"
                WHERE sponsor_name IS NOT NULL
                  AND sponsor_name::text NOT IN ('null','[]','')
                  AND issue_date IS NOT NULL
            ),
            articles AS (
                SELECT
                    id,
                    issue_date,
                    RTRIM(REGEXP_REPLACE(
                        LOWER(SPLIT_PART(SPLIT_PART(url,'?',1),'#',1)),
                        '^https?://(www\\.)?',''),'/')       AS url_key,
                    SPLIT_PART(RTRIM(REGEXP_REPLACE(
                        LOWER(SPLIT_PART(SPLIT_PART(url,'?',1),'#',1)),
                        '^https?://(www\\.)?',''),'/'),
                    '/',1)                                   AS url_domain
                FROM {sa}."{ta}"
                WHERE issue_date IS NOT NULL
                {issue_filter}
            ),
            matched AS (
                SELECT
                    a.id,
                    COALESCE(
                        MAX(CASE WHEN s1.sponsor_url_key = a.url_key
                             AND s1.issue_date = a.issue_date
                             AND s1.sponsor_url_key <> ''
                            THEN s1.sponsor_name_text END),
                        MAX(CASE WHEN s2.affiliate_url_key = a.url_key
                             AND s2.issue_date = a.issue_date
                             AND s2.affiliate_url_key <> ''
                            THEN s2.sponsor_name_text END),
                        MAX(CASE WHEN s3.sponsor_domain = a.url_domain
                             AND s3.issue_date = a.issue_date
                             AND s3.sponsor_domain <> ''
                            THEN s3.sponsor_name_text END),
                        MAX(CASE WHEN s4.affiliate_domain = a.url_domain
                             AND s4.issue_date = a.issue_date
                             AND s4.affiliate_domain <> ''
                            THEN s4.sponsor_name_text END)
                    )                                        AS resolved_sponsor_name,
                    CASE
                        WHEN MAX(CASE WHEN s1.sponsor_url_key = a.url_key
                                  AND s1.issue_date = a.issue_date
                                  AND s1.sponsor_url_key <> ''
                                 THEN 1 END) = 1  THEN 'sponsor'
                        WHEN MAX(CASE WHEN s2.affiliate_url_key = a.url_key
                                  AND s2.issue_date = a.issue_date
                                  AND s2.affiliate_url_key <> ''
                                 THEN 1 END) = 1  THEN 'affiliate'
                        WHEN MAX(CASE WHEN s3.sponsor_domain = a.url_domain
                                  AND s3.issue_date = a.issue_date
                                  AND s3.sponsor_domain <> ''
                                 THEN 1 END) = 1  THEN 'sponsor'
                        WHEN MAX(CASE WHEN s4.affiliate_domain = a.url_domain
                                  AND s4.issue_date = a.issue_date
                                  AND s4.affiliate_domain <> ''
                                 THEN 1 END) = 1  THEN 'affiliate'
                        ELSE NULL
                    END                                      AS resolved_airtable_type
                FROM articles a
                LEFT JOIN sales s1
                    ON s1.sponsor_url_key = a.url_key
                   AND s1.issue_date = a.issue_date
                   AND s1.sponsor_url_key <> ''
                LEFT JOIN sales s2
                    ON s2.affiliate_url_key = a.url_key
                   AND s2.issue_date = a.issue_date
                   AND s2.affiliate_url_key <> ''
                LEFT JOIN sales s3
                    ON s3.sponsor_domain = a.url_domain
                   AND s3.issue_date = a.issue_date
                   AND s3.sponsor_domain <> ''
                LEFT JOIN sales s4
                    ON s4.affiliate_domain = a.url_domain
                   AND s4.issue_date = a.issue_date
                   AND s4.affiliate_domain <> ''
                GROUP BY a.id
            )
            UPDATE {sa}."{ta}" ac
            SET sponsor_name  = m.resolved_sponsor_name,
                airtable_type = m.resolved_airtable_type,
                updated_at    = NOW()
            FROM matched m
            WHERE ac.id = m.id
              AND m.resolved_sponsor_name IS NOT NULL
              AND m.resolved_sponsor_name <> ''
        """)
        n = cur.rowcount
    conn.commit()
    LOG.info("Enriched sponsor_name on %d article rows", n)
    return n


def _ensure_subscriber_columns(conn):
    """Add airtable_type column to articles_clicks if it doesn't exist yet."""
    sa, ta = ARTICLES_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            ALTER TABLE {sa}."{ta}"
            ADD COLUMN IF NOT EXISTS airtable_type TEXT
        """)
    conn.commit()


def _print_subscriber_box(conn):
    ss, ts = SUBSCRIBER_TABLE.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*)                AS total,
                SUM(unique_clicks)      AS total_unique,
                SUM(non_unique_clicks)  AS total_non_unique
            FROM {ss}."{ts}"
            WHERE non_unique_clicks > 0
        """)
        total, unique, non_unique = cur.fetchone()
    LOG.info("┌─────────────────────────────────────────┐")
    LOG.info("│         SUBSCRIBER CLICKS SUMMARY       │")
    LOG.info("├─────────────────────────────────────────┤")
    LOG.info("│  Total subscribers         : %10s │", f"{total:,}")
    LOG.info("│  Total unique clicks       : %10s │", f"{unique:,}")
    LOG.info("│  Total non-unique clicks   : %10s │", f"{non_unique:,}")
    LOG.info("└─────────────────────────────────────────┘")


def lambda_handler(event: dict, context: Any) -> dict:
    event      = event or {}
    started_at = datetime.now(timezone.utc)
    run_id     = getattr(context, "aws_request_id", None) or \
                 f"clicks-{started_at.strftime('%Y%m%d-%H%M%S')}"
    skip_airtable = str(event.get("skip_airtable", "false")).lower() in {"1", "true", "yes"}
    mode          = str(event.get("mode", "")).lower()

    LOG.info("Lambda run_id=%s  mode=%s  skip_airtable=%s", run_id, mode or "incremental", skip_airtable)

    conn = get_conn()

    # ── Full rebuild path — see run_full_rebuild()'s docstring. ────────
    if mode == "all":
        try:
            _ensure_subscriber_columns(conn)
            result = run_full_rebuild(conn, context)
            LOG.info("Rebuild step done: %s", result)
            return {"statusCode": 200, "body": json.dumps(result, default=str)}
        except Exception as exc:
            LOG.exception("Rebuild failed: %s", exc)
            conn.rollback()
            return {"statusCode": 500, "body": json.dumps({"error": str(exc)}, default=str)}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── Normal incremental path (unchanged) ────────────────────────────
    audit_id = None

    try:
        # ensure new columns exist (safe to run every time)
        _ensure_subscriber_columns(conn)

        window_start, window_end, up_to_date = get_window(conn, event)

        if up_to_date:
            LOG.info("Already up to date — nothing to process.")
            conn.close()
            return {"statusCode": 200, "body": json.dumps({"message": "up_to_date"})}

        audit_id = open_audit(conn, started_at, window_start, window_end)

        art_agg:  dict = {}
        sub_agg:  dict = {}
        total_source = total_kept = total_excl = batch_num = 0

        for batch in iter_clicks(conn, window_start, window_end):
            batch_num    += 1
            total_source += len(batch)
            kept, excl    = aggregate_batch(batch, art_agg, sub_agg)
            total_kept   += kept
            total_excl   += excl
            LOG.info("batch=%d source=%d kept=%d excl=%d art=%d sub=%d",
                     batch_num, total_source, total_kept, total_excl,
                     len(art_agg), len(sub_agg))

        if total_source == 0:
            LOG.info("No clicks in window %s → %s", window_start, window_end)
            close_audit(conn, audit_id, started_at, "success",
                        0, 0, 0, 0, 0, 0, {})
            conn.close()
            return {"statusCode": 200, "body": json.dumps({"message": "no_data"})}

        type_breakdown  = dict(Counter(v["type"] for v in art_agg.values()))
        article_rows    = upsert_articles(conn, art_agg)
        subscriber_rows = upsert_subscribers(conn, sub_agg)

        affected_issues = list({k[0] for k in art_agg.keys()})
        story_n = category_n = sponsor_n = 0
        if not skip_airtable:
            story_n, category_n = run_airtable_enrichment(conn, affected_issues)
            sponsor_n           = enrich_sponsor_names(conn)  # always full re-enrich

        close_audit(conn, audit_id, started_at, "success",
                    total_source, total_kept, article_rows, subscriber_rows,
                    story_n, category_n, type_breakdown)

        result = {
            "run_id":           run_id,
            "window_start":     str(window_start),
            "window_end":       str(window_end),
            "source_rows":      total_source,
            "kept_rows":        total_kept,
            "article_rows":     article_rows,
            "subscriber_rows":  subscriber_rows,
            "story_positions":  story_n,
            "pos_categories":   category_n,
            "sponsor_enriched": sponsor_n,
            "type_breakdown":   type_breakdown,
        }
        LOG.info("Done: %s", result)
        _print_subscriber_box(conn)
        return {"statusCode": 200, "body": json.dumps(result, default=str)}

    except Exception as exc:
        LOG.exception("Lambda failed: %s", exc)
        if audit_id:
            try:
                close_audit(conn, audit_id, started_at, "failed",
                            0, 0, 0, 0, 0, 0, {}, str(exc))
            except Exception:
                pass
        conn.rollback()
        return {"statusCode": 500,
                "body": json.dumps({"error": str(exc)}, default=str)}
    finally:
        try:
            conn.close()
        except Exception:
            pass
