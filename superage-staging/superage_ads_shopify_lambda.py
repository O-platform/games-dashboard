"""
SuperAge Dashboard — Ads + Shopify Metrics Refresh Lambda
=========================================================
Lambda name (prod): sa-dashboard-ads-shopify-metrics
  (renamed from sa-dashboard-ads-metrics — one lambda now builds BOTH the
   paid-media "Ads Performance" section and the new "Shopify Sales" section.)

Writes two independent JSON files to Cloudflare R2 (served to the dashboard
via the `dashboard.pardon-ventures-06b.workers.dev` Worker):

  • superage-dashboard/superage-ads.json      — Ads Performance (Meta today).
  • superage-dashboard/superage-shopify.json   — Shopify Sales (new section).

Keeping them as two files means the LIVE dashboard's Ads section keeps reading
the same `superage-ads.json` unchanged, while the Shopify section (still in the
preview `index.shopify.html`, not live yet) reads the new `superage-shopify.json`.

────────────────────────────────────────────────────────────────────────────
ADS  — source tables (Meta today; Google planned)
  • {schema}.meta_ad_totals        — one row per (campaign × ad-set) holding the
                                      totals for the current reporting period.
  • {schema}.meta_ad_performance    — daily grain (stat_date) per (campaign × ad-set).
  Conversion metric = custom_conversions (FB pixel custom conversion; the
  standard `leads` column is 0 for these lead-gen campaigns). Revenue / ROAS
  omitted (purchase_value is 0 — lead-gen, not e-commerce).

SHOPIFY  — source tables
  • {schema}.shopify_orders             — order grain. gross_sales, net_sales,
                                          total_discounts, total_shipping,
                                          total_tax, total_price, order_date_local,
                                          financial_status, fulfillment_status,
                                          cancelled_at, customer_id,
                                          customer_orders_count.
  • {schema}.shopify_order_line_items    — line-item grain. vendor, product_type,
                                          title, sku, quantity, gross_sales.
  Revenue definitions (verified against the data):
     net_sales   = gross_sales − discounts       (product net revenue)
     total_price = gross − discounts + shipping + tax   (what the customer paid)
  KPIs exclude cancelled orders (cancelled_at IS NOT NULL).

Output JSON shapes
  superage-ads.json:
    { "ads_as_of": "...", "ads": { "meta": { window, kpis, daily, by_campaign, by_goal } } }

  superage-shopify.json:
    { "shopify_as_of": "...",
      "shopify": {
        "window":          { start, end, days },
        "kpis":            { orders, gross_sales, net_sales, total_discounts,
                             total_shipping, total_tax, revenue, units, aov,
                             unique_customers, repeat_customers, refunded_orders,
                             cancelled_orders },
        "daily":           { dates[], net_sales[], gross_sales[], orders[] },
        "monthly":         { months[], net_sales[], gross_sales[], orders[] },
        "by_vendor":       [ { vendor, gross_sales, units, orders, aov } ],
        "by_product_type": [ { product_type, gross_sales, units, orders } ],
        "top_products":    [ { title, vendor, gross_sales, units, orders } ],
        "recent_orders":   [ { name, order_date, financial_status,
                               fulfillment_status, gross_sales, net_sales, revenue } ]
      } }

Required env vars:
  DB_SECRET_ARN   — Secrets Manager ARN (JSON: host/port/dbname/username/password)
  R2_SECRET_ARN   — Secrets Manager ARN; keys: account_id, access_key_id,
                    secret_access_key, bucket_name

Optional env vars:
  DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_SSLMODE
  R2_FILE_PATH          (default: superage-dashboard/superage-ads.json)
  R2_SHOPIFY_FILE_PATH  (default: superage-dashboard/superage-shopify.json)
  SA_SCHEMA             (default: superage)
  ADS_WINDOW_DAYS       (default: 90  — ads daily-trend lookback)
  SHOPIFY_WINDOW_DAYS   (default: 365 — shopify daily-trend / KPI window)
  WRITE_TO_R2           (default: true; set false for local/test run)

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

R2_FILE_PATH         = os.environ.get("R2_FILE_PATH", "superage-dashboard/superage-ads.json")
R2_SHOPIFY_FILE_PATH = os.environ.get("R2_SHOPIFY_FILE_PATH", "superage-dashboard/superage-shopify.json")
SA_SCHEMA            = os.environ.get("SA_SCHEMA", "superage")
WINDOW_DAYS          = int(os.environ.get("ADS_WINDOW_DAYS", "90"))
SHOPIFY_WINDOW_DAYS  = int(os.environ.get("SHOPIFY_WINDOW_DAYS", "365"))
WRITE_TO_R2          = os.environ.get("WRITE_TO_R2", "true").strip().lower() not in {"0", "false", "no"}


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


def write_to_r2(content: str, key: str = None):
    """Uploads the JSON string to R2 under `key`. No-op when WRITE_TO_R2=false
    (local dev). Defaults to the ads key for backwards compatibility."""
    key = key or R2_FILE_PATH
    if not WRITE_TO_R2:
        logger.info("WRITE_TO_R2=false — skipping R2 upload for key=%s.", key)
        return {"written": False, "skipped": True, "key": key}
    try:
        client, bucket = _get_r2_client()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-store",
        )
        logger.info("R2 write OK — bucket=%s key=%s", bucket, key)
        return {"written": True, "bucket": bucket, "key": key}
    except Exception as e:
        logger.error("R2 write failed (key=%s): %s", key, e)
        return {"written": False, "error": str(e), "key": key}


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
# Ads section builder  (unchanged logic from the old ads lambda)
# ─────────────────────────────────────────────────────────────

def build_ads(cur, S, _t):
    meta = {}

    # meta_ad_totals — latest reporting-period snapshot. Every row in a load
    # shares the same period_end; taking MAX keeps only the most recent snapshot.
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
    _t("ads 1. account KPIs")

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

    # 2. Daily trend — from meta_ad_performance (daily grain).
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
    _t("ads 2. daily trend")

    meta["daily"] = {
        "dates":              [str(r["stat_date"]) for r in daily_rows],
        "spend":              [round(safe_float(r["spend"]), 2) for r in daily_rows],
        "impressions":        [safe_int(r["impressions"]) for r in daily_rows],
        "clicks":             [safe_int(r["clicks"]) for r in daily_rows],
        "link_clicks":        [safe_int(r["link_clicks"]) for r in daily_rows],
        "conversions":        [round(safe_float(r["conversions"]), 2) for r in daily_rows],
        "landing_page_views": [safe_int(r["landing_page_views"]) for r in daily_rows],
    }

    # 3. By campaign — roll ad-sets up to the campaign.
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
    _t("ads 3. by campaign")

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

    # 4. By optimization goal.
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
    _t("ads 4. by optimization goal")

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

    return meta


# ─────────────────────────────────────────────────────────────
# Shopify section builder  (new)
# ─────────────────────────────────────────────────────────────

def build_shopify(cur, S, _t):
    """Builds the Shopify Sales payload from shopify_orders +
    shopify_order_line_items. All KPI / breakdown queries exclude cancelled
    orders. Revenue = total_price (what the customer paid); net_sales =
    product revenue after discounts; gross_sales = product revenue pre-discount."""
    shop = {}
    W = int(SHOPIFY_WINDOW_DAYS)

    # Order-level filter reused everywhere (window + not cancelled).
    order_filter = (
        f"cancelled_at IS NULL "
        f"AND order_date_local >= CURRENT_DATE - INTERVAL '{W} days'"
    )

    # 1. Headline KPIs (order grain).
    cur.execute(f"""
        SELECT
            COUNT(*)                                     AS orders,
            MIN(order_date_local)                        AS first_date,
            MAX(order_date_local)                        AS last_date,
            COALESCE(SUM(gross_sales), 0)                AS gross_sales,
            COALESCE(SUM(net_sales), 0)                  AS net_sales,
            COALESCE(SUM(total_discounts), 0)            AS total_discounts,
            COALESCE(SUM(total_shipping), 0)             AS total_shipping,
            COALESCE(SUM(total_tax), 0)                  AS total_tax,
            COALESCE(SUM(total_price), 0)                AS revenue,
            COUNT(DISTINCT customer_id)                  AS unique_customers,
            COUNT(*) FILTER (
                WHERE UPPER(COALESCE(financial_status,'')) LIKE '%REFUND%'
            )                                            AS refunded_orders
        FROM {S}.shopify_orders
        WHERE {order_filter}
    """)
    k = cur.fetchone() or {}
    _t("shopify 1. KPIs")

    # 1b. Units (line-item grain, same order filter).
    cur.execute(f"""
        SELECT COALESCE(SUM(li.quantity), 0) AS units
        FROM {S}.shopify_order_line_items li
        JOIN {S}.shopify_orders o ON o.order_id = li.order_id
        WHERE o.{order_filter}
    """)
    units = safe_int((cur.fetchone() or {}).get("units"))

    # 1c. Cancelled orders (in window, ignoring the not-cancelled filter).
    cur.execute(f"""
        SELECT COUNT(*) AS cancelled_orders
        FROM {S}.shopify_orders
        WHERE cancelled_at IS NOT NULL
          AND order_date_local >= CURRENT_DATE - INTERVAL '{W} days'
    """)
    cancelled_orders = safe_int((cur.fetchone() or {}).get("cancelled_orders"))

    # 1d. Repeat customers — all-time customers with 2+ non-cancelled orders.
    cur.execute(f"""
        SELECT COUNT(*) AS repeat_customers FROM (
            SELECT customer_id
            FROM {S}.shopify_orders
            WHERE customer_id IS NOT NULL AND cancelled_at IS NULL
            GROUP BY customer_id
            HAVING COUNT(*) >= 2
        ) t
    """)
    repeat_customers = safe_int((cur.fetchone() or {}).get("repeat_customers"))

    orders     = safe_int(k.get("orders"))
    net_sales  = round(safe_float(k.get("net_sales")), 2)
    revenue    = round(safe_float(k.get("revenue")), 2)

    shop["window"] = {
        "start": str(k.get("first_date")) if k.get("first_date") else None,
        "end":   str(k.get("last_date")) if k.get("last_date") else None,
        "days":  W,
    }
    shop["kpis"] = {
        "orders":           orders,
        "gross_sales":      round(safe_float(k.get("gross_sales")), 2),
        "net_sales":        net_sales,
        "total_discounts":  round(safe_float(k.get("total_discounts")), 2),
        "total_shipping":   round(safe_float(k.get("total_shipping")), 2),
        "total_tax":        round(safe_float(k.get("total_tax")), 2),
        "revenue":          revenue,
        "units":            units,
        "aov":              rate(net_sales, orders, 1.0, 2),          # net sales ÷ order
        "revenue_per_order": rate(revenue, orders, 1.0, 2),          # total paid ÷ order
        "unique_customers": safe_int(k.get("unique_customers")),
        "repeat_customers": repeat_customers,
        "refunded_orders":  safe_int(k.get("refunded_orders")),
        "cancelled_orders": cancelled_orders,
    }

    # 2. Daily trend (line chart to identify sales trends).
    cur.execute(f"""
        SELECT
            order_date_local                AS d,
            COUNT(*)                        AS orders,
            COALESCE(SUM(gross_sales), 0)   AS gross_sales,
            COALESCE(SUM(net_sales), 0)     AS net_sales
        FROM {S}.shopify_orders
        WHERE {order_filter}
        GROUP BY order_date_local
        ORDER BY order_date_local
    """)
    daily_rows = cur.fetchall()
    _t("shopify 2. daily trend")

    shop["daily"] = {
        "dates":       [str(r["d"]) for r in daily_rows],
        "orders":      [safe_int(r["orders"]) for r in daily_rows],
        "gross_sales": [round(safe_float(r["gross_sales"]), 2) for r in daily_rows],
        "net_sales":   [round(safe_float(r["net_sales"]), 2) for r in daily_rows],
    }

    # 3. Monthly trend (all-time, non-cancelled).
    cur.execute(f"""
        SELECT
            to_char(date_trunc('month', order_date_local), 'YYYY-MM') AS m,
            COUNT(*)                        AS orders,
            COALESCE(SUM(gross_sales), 0)   AS gross_sales,
            COALESCE(SUM(net_sales), 0)     AS net_sales
        FROM {S}.shopify_orders
        WHERE cancelled_at IS NULL
        GROUP BY 1
        ORDER BY 1
    """)
    month_rows = cur.fetchall()
    _t("shopify 3. monthly trend")

    shop["monthly"] = {
        "months":      [r["m"] for r in month_rows],
        "orders":      [safe_int(r["orders"]) for r in month_rows],
        "gross_sales": [round(safe_float(r["gross_sales"]), 2) for r in month_rows],
        "net_sales":   [round(safe_float(r["net_sales"]), 2) for r in month_rows],
    }

    # 4. By vendor (line-item grain, windowed, non-cancelled orders).
    cur.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(li.vendor), ''), '—')  AS vendor,
            COALESCE(SUM(li.gross_sales), 0)            AS gross_sales,
            COALESCE(SUM(li.quantity), 0)               AS units,
            COUNT(DISTINCT li.order_id)                 AS orders
        FROM {S}.shopify_order_line_items li
        JOIN {S}.shopify_orders o ON o.order_id = li.order_id
        WHERE o.{order_filter}
        GROUP BY 1
        ORDER BY gross_sales DESC
    """)
    vendor_rows = cur.fetchall()
    _t("shopify 4. by vendor")

    shop["by_vendor"] = [
        {
            "vendor":      r.get("vendor") or "—",
            "gross_sales": round(safe_float(r.get("gross_sales")), 2),
            "units":       safe_int(r.get("units")),
            "orders":      safe_int(r.get("orders")),
            "aov":         rate(r.get("gross_sales"), r.get("orders"), 1.0, 2),
        }
        for r in vendor_rows
    ]

    # 5. By product type.
    cur.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(li.product_type), ''), '—')  AS product_type,
            COALESCE(SUM(li.gross_sales), 0)                  AS gross_sales,
            COALESCE(SUM(li.quantity), 0)                     AS units,
            COUNT(DISTINCT li.order_id)                       AS orders
        FROM {S}.shopify_order_line_items li
        JOIN {S}.shopify_orders o ON o.order_id = li.order_id
        WHERE o.{order_filter}
        GROUP BY 1
        ORDER BY gross_sales DESC
    """)
    ptype_rows = cur.fetchall()
    _t("shopify 5. by product type")

    shop["by_product_type"] = [
        {
            "product_type": r.get("product_type") or "—",
            "gross_sales":  round(safe_float(r.get("gross_sales")), 2),
            "units":        safe_int(r.get("units")),
            "orders":       safe_int(r.get("orders")),
        }
        for r in ptype_rows
    ]

    # 6. Top products (by title + vendor).
    cur.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(li.title), ''), '—')   AS title,
            COALESCE(NULLIF(TRIM(li.vendor), ''), '—')  AS vendor,
            COALESCE(SUM(li.gross_sales), 0)            AS gross_sales,
            COALESCE(SUM(li.quantity), 0)               AS units,
            COUNT(DISTINCT li.order_id)                 AS orders
        FROM {S}.shopify_order_line_items li
        JOIN {S}.shopify_orders o ON o.order_id = li.order_id
        WHERE o.{order_filter}
        GROUP BY 1, 2
        ORDER BY gross_sales DESC
        LIMIT 25
    """)
    prod_rows = cur.fetchall()
    _t("shopify 6. top products")

    shop["top_products"] = [
        {
            "title":       r.get("title") or "—",
            "vendor":      r.get("vendor") or "—",
            "gross_sales": round(safe_float(r.get("gross_sales")), 2),
            "units":       safe_int(r.get("units")),
            "orders":      safe_int(r.get("orders")),
        }
        for r in prod_rows
    ]

    # 7. Recent orders.
    cur.execute(f"""
        SELECT
            name,
            order_date_local,
            financial_status,
            fulfillment_status,
            COALESCE(gross_sales, 0)  AS gross_sales,
            COALESCE(net_sales, 0)    AS net_sales,
            COALESCE(total_price, 0)  AS revenue
        FROM {S}.shopify_orders
        WHERE cancelled_at IS NULL
        ORDER BY created_at DESC
        LIMIT 25
    """)
    recent_rows = cur.fetchall()
    _t("shopify 7. recent orders")

    shop["recent_orders"] = [
        {
            "name":               r.get("name") or "—",
            "order_date":         str(r.get("order_date_local")) if r.get("order_date_local") else "—",
            "financial_status":   r.get("financial_status") or "—",
            "fulfillment_status": r.get("fulfillment_status") or "—",
            "gross_sales":        round(safe_float(r.get("gross_sales")), 2),
            "net_sales":          round(safe_float(r.get("net_sales")), 2),
            "revenue":            round(safe_float(r.get("revenue")), 2),
        }
        for r in recent_rows
    ]

    return shop


# ─────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    import time as _time
    S = SA_SCHEMA
    logger.info(
        "SuperAge ads+shopify Lambda starting — ads_key=%s shopify_key=%s schema=%s "
        "ads_window=%dd shopify_window=%dd",
        R2_FILE_PATH, R2_SHOPIFY_FILE_PATH, S, WINDOW_DAYS, SHOPIFY_WINDOW_DAYS,
    )
    _t0 = _time.time()

    def _t(label):
        logger.info("  ⏱  %-45s  %.1fs", label, _time.time() - _t0)

    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    meta = {}
    shop = {}
    try:
        meta = build_ads(cur, S, _t)
        shop = build_shopify(cur, S, _t)
    finally:
        cur.close()
        conn.close()

    ads_payload = {
        "ads_as_of": _date_label(),
        "ads": {
            "meta": meta,
            # "google": {}  ← future channel slots in here
        },
    }
    shopify_payload = {
        "shopify_as_of": _date_label(),
        "shopify": shop,
    }

    ads_r2     = write_to_r2(json.dumps(ads_payload, indent=2, default=str), R2_FILE_PATH)
    shopify_r2 = write_to_r2(json.dumps(shopify_payload, indent=2, default=str), R2_SHOPIFY_FILE_PATH)

    logger.info(
        "Ads done — spend=%.2f conversions=%.0f campaigns=%d daily_points=%d",
        safe_float(meta.get("kpis", {}).get("spend")),
        safe_float(meta.get("kpis", {}).get("conversions")),
        safe_int(meta.get("kpis", {}).get("total_campaigns")),
        len(meta.get("daily", {}).get("dates", [])),
    )
    logger.info(
        "Shopify done — orders=%d net_sales=%.2f revenue=%.2f vendors=%d daily_points=%d",
        safe_int(shop.get("kpis", {}).get("orders")),
        safe_float(shop.get("kpis", {}).get("net_sales")),
        safe_float(shop.get("kpis", {}).get("revenue")),
        len(shop.get("by_vendor", [])),
        len(shop.get("daily", {}).get("dates", [])),
    )
    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "ads_r2":     ads_r2,
            "shopify_r2": shopify_r2,
            "ads_as_of":  ads_payload["ads_as_of"],
            "spend":            meta.get("kpis", {}).get("spend"),
            "conversions":      meta.get("kpis", {}).get("conversions"),
            "total_campaigns":  meta.get("kpis", {}).get("total_campaigns"),
            "shopify_orders":   shop.get("kpis", {}).get("orders"),
            "shopify_net_sales": shop.get("kpis", {}).get("net_sales"),
            "shopify_revenue":  shop.get("kpis", {}).get("revenue"),
        }),
    }


if __name__ == "__main__":
    import pprint
    result = lambda_handler({}, None)
    pprint.pprint(result)
