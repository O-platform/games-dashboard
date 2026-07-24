"""
SuperAge Dashboard — superage-ads.json Refresh Lambda
=====================================================
Lambda name (prod): sa-dashboard-ads-metrics

Builds the "Ads Performance" section of the SuperAge dashboard from the
paid-media tables and uploads `superage-dashboard/superage-ads.json` to
Cloudflare R2 (served to the dashboard via the
`dashboard.pardon-ventures-06b.workers.dev` Worker).

Source tables (Meta today; Google planned):
  • {schema}.meta_ad_totals        — one row per (campaign × ad-set) holding the
                                      totals for the current reporting period
                                      (period_start … period_end). Used for the
                                      headline KPIs, the campaign breakdown, and
                                      the optimization-goal split.
  • {schema}.meta_ad_performance    — daily grain (stat_date) per
                                      (campaign × ad-set). Used for the daily
                                      spend / conversions / clicks trend.

Conversion metric:
  This account runs lead-gen campaigns whose real conversion fires on the FB
  pixel custom conversion, surfaced in `custom_conversions` (the standard
  `leads` column is 0 for these campaigns). So the primary "Conversions" KPI
  and cost-per-conversion are based on custom_conversions. Revenue / ROAS is
  intentionally omitted (purchase_value is 0 — this is lead-gen, not e-commerce).

Output JSON shape (channel-agnostic so Google can be added later):
  {
    "ads_as_of": "Jul 24, 2026",
    "ads": {
      "meta": {
        "window":   { "start": "...", "end": "...", "days": 90 },
        "kpis":     { spend, impressions, reach, frequency, clicks, link_clicks,
                      ctr, link_ctr, cpc, cpm, landing_page_views, conversions,
                      cost_per_conversion, purchases, leads, add_to_cart,
                      initiate_checkout, total_campaigns, active_campaigns },
        "daily":    { dates[], spend[], impressions[], clicks[], link_clicks[],
                      conversions[], landing_page_views[] },
        "by_campaign": [ { campaign_name, delivery_status, optimization_goal,
                           spend, impressions, reach, clicks, link_clicks, ctr,
                           cpc, cpm, landing_page_views, conversions,
                           cost_per_conversion } ],
        "by_goal":  [ { goal, spend, conversions, cost_per_conversion,
                        link_clicks, impressions } ]
      }
      // "google": { ... }  ← future
    }
  }

Required env vars:
  DB_SECRET_ARN   — Secrets Manager ARN (JSON: host/port/dbname/username/password)
  R2_SECRET_ARN   — Secrets Manager ARN; secret must carry the keys
                    account_id, access_key_id, secret_access_key, bucket_name

Optional env vars:
  DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_SSLMODE
  R2_FILE_PATH     (default: superage-dashboard/superage-ads.json)
  SA_SCHEMA        (default: superage)
  ADS_WINDOW_DAYS  (default: 90 — daily-trend lookback)
  WRITE_TO_R2      (default: true; set false for local/test run)

Runtime: Python 3.12 | Layer: psycopg2
"""

import json
import logging
import os
from datetime import date

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_db_secret_cache = None
_r2_secret_cache = None
_r2_client_cache = None

R2_FILE_PATH    = os.environ.get("R2_FILE_PATH", "superage-dashboard/superage-ads.json")
SA_SCHEMA       = os.environ.get("SA_SCHEMA", "superage")
WINDOW_DAYS     = int(os.environ.get("ADS_WINDOW_DAYS", "90"))
WRITE_TO_R2     = os.environ.get("WRITE_TO_R2", "true").strip().lower() not in {"0", "false", "no"}


# ─────────────────────────────────────────────────────────────
# R2 helpers  (identical pattern to the metrics/comparison lambdas)
# ─────────────────────────────────────────────────────────────

def _date_label() -> str:
    return date.today().strftime("%b %d, %Y").replace(" 0", " ")


def _get_r2_client():
    """Cached (boto3 S3 client pointed at R2, bucket_name) tuple."""
    global _r2_client_cache, _r2_secret_cache
    if _r2_client_cache is not None:
        return _r2_client_cache

    arn = os.environ["R2_SECRET_ARN"]
    sm  = boto3.client(
        "secretsmanager",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    _r2_secret_cache = json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{_r2_secret_cache['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=_r2_secret_cache["access_key_id"],
        aws_secret_access_key=_r2_secret_cache["secret_access_key"],
        region_name="auto",
    )
    _r2_client_cache = (client, _r2_secret_cache["bucket_name"])
    return _r2_client_cache


def write_to_r2(content: str):
    """Uploads the JSON string to R2. No-op when WRITE_TO_R2=false (local dev)."""
    if not WRITE_TO_R2:
        logger.info("WRITE_TO_R2=false — skipping R2 upload.")
        return {"written": False, "skipped": True}
    try:
        client, bucket = _get_r2_client()
        client.put_object(
            Bucket=bucket,
            Key=R2_FILE_PATH,
            Body=content.encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-store",
        )
        logger.info("R2 write OK — bucket=%s key=%s", bucket, R2_FILE_PATH)
        return {"written": True, "bucket": bucket, "key": R2_FILE_PATH}
    except Exception as e:
        logger.error("R2 write failed: %s", e)
        return {"written": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# DB helpers  (identical pattern to the metrics/comparison lambdas)
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


def rate(numer, denom, scale=1.0, decimals=4):
    """Safe ratio: numer/denom*scale, 0.0 when denom is falsy."""
    n = safe_float(numer)
    d = safe_float(denom)
    if not d:
        return 0.0
    return round(n / d * scale, decimals)


def _derive_rates(k: dict) -> dict:
    """Recompute rate metrics from summed base metrics (never average per-row
    rates — that double-weights small ad-sets). Mutates and returns k."""
    spend       = safe_float(k.get("spend"))
    impressions = safe_int(k.get("impressions"))
    reach       = safe_int(k.get("reach"))
    clicks      = safe_int(k.get("clicks"))
    link_clicks = safe_int(k.get("link_clicks"))
    conversions = safe_float(k.get("conversions"))

    k["ctr"]                 = rate(clicks, impressions, 100.0, 4)       # all clicks / impr
    k["link_ctr"]            = rate(link_clicks, impressions, 100.0, 4)  # link clicks / impr
    k["cpc"]                 = rate(spend, clicks, 1.0, 4)               # cost / all click
    k["cpm"]                 = rate(spend, impressions, 1000.0, 4)       # cost / 1000 impr
    k["frequency"]           = rate(impressions, reach, 1.0, 4)
    k["cost_per_conversion"] = rate(spend, conversions, 1.0, 4)
    return k


# ─────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    import time as _time
    S = SA_SCHEMA
    logger.info("SuperAge ads Lambda starting — r2_key=%s schema=%s window=%dd",
                R2_FILE_PATH, S, WINDOW_DAYS)
    _t0 = _time.time()

    def _t(label):
        logger.info("  ⏱  %-45s  %.1fs", label, _time.time() - _t0)

    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    meta = {}
    try:
        # ─────────────────────────────────────────────────────
        # meta_ad_totals — latest reporting-period snapshot.
        # Every row in a load shares the same period_end; taking MAX keeps only
        # the most recent snapshot in case older ones linger in the table.
        # ─────────────────────────────────────────────────────
        snap_cte = f"""
            WITH snap AS (
                SELECT *
                FROM {S}.meta_ad_totals
                WHERE period_end = (SELECT MAX(period_end) FROM {S}.meta_ad_totals)
            )
        """

        # 1. Window + account-level KPIs
        cur.execute(f"""
            {snap_cte}
            SELECT
                MIN(period_start)                                        AS period_start,
                MAX(period_end)                                          AS period_end,
                COUNT(DISTINCT campaign_id)                              AS total_campaigns,
                COUNT(DISTINCT campaign_id) FILTER (
                    WHERE UPPER(COALESCE(delivery_status,'')) = 'ACTIVE') AS active_campaigns,
                COALESCE(SUM(spend), 0)                                  AS spend,
                COALESCE(SUM(impressions), 0)                            AS impressions,
                COALESCE(SUM(reach), 0)                                  AS reach,
                COALESCE(SUM(clicks), 0)                                 AS clicks,
                COALESCE(SUM(link_clicks), 0)                            AS link_clicks,
                COALESCE(SUM(landing_page_views), 0)                     AS landing_page_views,
                COALESCE(SUM(custom_conversions), 0)                     AS conversions,
                COALESCE(SUM(purchases), 0)                              AS purchases,
                COALESCE(SUM(purchase_value), 0)                         AS purchase_value,
                COALESCE(SUM(leads), 0)                                  AS leads,
                COALESCE(SUM(add_to_cart), 0)                            AS add_to_cart,
                COALESCE(SUM(initiate_checkout), 0)                      AS initiate_checkout
            FROM snap
        """)
        row = cur.fetchone() or {}
        _t("1. account KPIs")

        p_start = row.get("period_start")
        p_end   = row.get("period_end")
        window_days = (p_end - p_start).days if (p_start and p_end) else WINDOW_DAYS

        kpis = {
            "spend":               round(safe_float(row.get("spend")), 2),
            "impressions":         safe_int(row.get("impressions")),
            "reach":               safe_int(row.get("reach")),
            "clicks":              safe_int(row.get("clicks")),
            "link_clicks":         safe_int(row.get("link_clicks")),
            "landing_page_views":  safe_int(row.get("landing_page_views")),
            "conversions":         round(safe_float(row.get("conversions")), 2),
            "purchases":           round(safe_float(row.get("purchases")), 2),
            "purchase_value":      round(safe_float(row.get("purchase_value")), 2),
            "leads":               safe_int(row.get("leads")),
            "add_to_cart":         safe_int(row.get("add_to_cart")),
            "initiate_checkout":   safe_int(row.get("initiate_checkout")),
            "total_campaigns":     safe_int(row.get("total_campaigns")),
            "active_campaigns":    safe_int(row.get("active_campaigns")),
        }
        _derive_rates(kpis)

        meta["window"] = {
            "start": str(p_start) if p_start else None,
            "end":   str(p_end) if p_end else None,
            "days":  window_days,
        }
        meta["kpis"] = kpis

        # ─────────────────────────────────────────────────────
        # 2. Daily trend — from meta_ad_performance (daily grain).
        # ─────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                stat_date,
                COALESCE(SUM(spend), 0)              AS spend,
                COALESCE(SUM(impressions), 0)        AS impressions,
                COALESCE(SUM(clicks), 0)             AS clicks,
                COALESCE(SUM(link_clicks), 0)        AS link_clicks,
                COALESCE(SUM(custom_conversions), 0) AS conversions,
                COALESCE(SUM(landing_page_views), 0) AS landing_page_views
            FROM {S}.meta_ad_performance
            WHERE stat_date >= CURRENT_DATE - INTERVAL '{int(WINDOW_DAYS)} days'
            GROUP BY stat_date
            ORDER BY stat_date
        """)
        daily_rows = cur.fetchall()
        _t("2. daily trend")

        meta["daily"] = {
            "dates":              [str(r["stat_date"]) for r in daily_rows],
            "spend":              [round(safe_float(r["spend"]), 2) for r in daily_rows],
            "impressions":        [safe_int(r["impressions"]) for r in daily_rows],
            "clicks":             [safe_int(r["clicks"]) for r in daily_rows],
            "link_clicks":        [safe_int(r["link_clicks"]) for r in daily_rows],
            "conversions":        [round(safe_float(r["conversions"]), 2) for r in daily_rows],
            "landing_page_views": [safe_int(r["landing_page_views"]) for r in daily_rows],
        }

        # ─────────────────────────────────────────────────────
        # 3. By campaign — roll ad-sets up to the campaign.
        # ─────────────────────────────────────────────────────
        cur.execute(f"""
            {snap_cte}
            SELECT
                campaign_id,
                MAX(campaign_name)                                       AS campaign_name,
                CASE WHEN bool_or(UPPER(COALESCE(delivery_status,'')) = 'ACTIVE')
                     THEN 'ACTIVE' ELSE 'PAUSED' END                     AS delivery_status,
                MAX(optimization_goal)                                   AS optimization_goal,
                COALESCE(SUM(spend), 0)                                  AS spend,
                COALESCE(SUM(impressions), 0)                            AS impressions,
                COALESCE(SUM(reach), 0)                                  AS reach,
                COALESCE(SUM(clicks), 0)                                 AS clicks,
                COALESCE(SUM(link_clicks), 0)                            AS link_clicks,
                COALESCE(SUM(landing_page_views), 0)                     AS landing_page_views,
                COALESCE(SUM(custom_conversions), 0)                     AS conversions
            FROM snap
            GROUP BY campaign_id
            ORDER BY spend DESC
        """)
        camp_rows = cur.fetchall()
        _t("3. by campaign")

        by_campaign = []
        for r in camp_rows:
            c = {
                "campaign_name":      (r.get("campaign_name") or "—"),
                "delivery_status":    r.get("delivery_status") or "—",
                "optimization_goal":  r.get("optimization_goal") or "—",
                "spend":              round(safe_float(r.get("spend")), 2),
                "impressions":        safe_int(r.get("impressions")),
                "reach":              safe_int(r.get("reach")),
                "clicks":             safe_int(r.get("clicks")),
                "link_clicks":        safe_int(r.get("link_clicks")),
                "landing_page_views": safe_int(r.get("landing_page_views")),
                "conversions":        round(safe_float(r.get("conversions")), 2),
            }
            _derive_rates(c)
            by_campaign.append(c)
        meta["by_campaign"] = by_campaign

        # ─────────────────────────────────────────────────────
        # 4. By optimization goal.
        # ─────────────────────────────────────────────────────
        cur.execute(f"""
            {snap_cte}
            SELECT
                COALESCE(NULLIF(TRIM(optimization_goal), ''), '—')  AS goal,
                COALESCE(SUM(spend), 0)                             AS spend,
                COALESCE(SUM(custom_conversions), 0)                AS conversions,
                COALESCE(SUM(link_clicks), 0)                       AS link_clicks,
                COALESCE(SUM(impressions), 0)                       AS impressions
            FROM snap
            GROUP BY 1
            ORDER BY spend DESC
        """)
        goal_rows = cur.fetchall()
        _t("4. by optimization goal")

        meta["by_goal"] = [
            {
                "goal":                r.get("goal") or "—",
                "spend":               round(safe_float(r.get("spend")), 2),
                "conversions":         round(safe_float(r.get("conversions")), 2),
                "cost_per_conversion": rate(r.get("spend"), r.get("conversions"), 1.0, 4),
                "link_clicks":         safe_int(r.get("link_clicks")),
                "impressions":         safe_int(r.get("impressions")),
            }
            for r in goal_rows
        ]

    finally:
        cur.close()
        conn.close()

    payload = {
        "ads_as_of": _date_label(),
        "ads": {
            "meta": meta,
            # "google": {}  ← future channel slots in here
        },
    }

    body = json.dumps(payload, indent=2, default=str)
    r2_result = write_to_r2(body)

    logger.info(
        "Ads done — spend=%.2f conversions=%.0f campaigns=%d daily_points=%d",
        safe_float(meta.get("kpis", {}).get("spend")),
        safe_float(meta.get("kpis", {}).get("conversions")),
        safe_int(meta.get("kpis", {}).get("total_campaigns")),
        len(meta.get("daily", {}).get("dates", [])),
    )
    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "r2":     r2_result,
            "ads_as_of": payload["ads_as_of"],
            "spend": meta.get("kpis", {}).get("spend"),
            "conversions": meta.get("kpis", {}).get("conversions"),
            "total_campaigns": meta.get("kpis", {}).get("total_campaigns"),
        }),
    }


if __name__ == "__main__":
    import pprint
    result = lambda_handler({}, None)
    pprint.pprint(result)
