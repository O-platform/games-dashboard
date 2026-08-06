"""
Local development runner for the SuperAge lambdas.

Runs superage_metrics_lambda_updated.py, superage_comparison_lambda.py, and
superage_ads_shopify_lambda.py using DB credentials from environment variables
(no AWS Secrets Manager), then writes the JSON outputs to local files read by
index.html (auto-detects localhost) and index.shopify.html (preview).

Usage:
    export DB_HOST=your-rds-host.rds.amazonaws.com
    export DB_PORT=5432          # default 5432
    export DB_NAME=your_db
    export DB_USER=your_user
    export DB_PASSWORD=your_password
    export SA_SCHEMA=superage    # default: superage
    export DB_SSLMODE=require    # default: require

    cd superage-staging/
    python run_local.py

    # Then serve the directory and open the local dashboard:
    python -m http.server 8080
    # Visit: http://localhost:8080/index.html          (live sections)
    #        http://localhost:8080/index.shopify.html  (preview: + Shopify Sales)

Outputs written here:
    superage-staging/superage-metrics.json
    superage-staging/superage-comparison.json
    superage-staging/superage-ads.json
    superage-staging/superage-shopify.json
"""

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

HERE = Path(__file__).parent

# ── env defaults ──
os.environ.setdefault("WRITE_TO_R2", "false")
os.environ.setdefault("SA_SCHEMA", "superage")
os.environ.setdefault("DB_SSLMODE", "require")

# ── DB credentials (do not commit with real values) ──
os.environ.setdefault("DB_HOST",     "powerbi.ctqeq4e88wx8.us-west-1.rds.amazonaws.com")
os.environ.setdefault("DB_PORT",     "5432")
os.environ.setdefault("DB_NAME",     "postgres")
os.environ.setdefault("DB_USER",     "postgres")
os.environ.setdefault("DB_PASSWORD", "PostgresAdmin1234")


def _local_db_secret():
    """Returns a secret dict built from env vars — no AWS call."""
    required = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"\nMissing required env vars: {', '.join(missing)}\n"
            "Set DB_HOST, DB_NAME, DB_USER, DB_PASSWORD and re-run.\n"
        )
    return {
        "host":     os.environ["DB_HOST"],
        "port":     int(os.environ.get("DB_PORT", "5432")),
        "dbname":   os.environ["DB_NAME"],
        "username": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def _write_local(content: str, filename: str):
    out = HERE / filename
    out.write_text(content, encoding="utf-8")
    log.info("Wrote %s  (%.1f KB)", out.name, len(content) / 1024)


def run_metrics_lambda():
    import superage_metrics_lambda_updated as lm

    def _fake_write(content: str):
        _write_local(content, "superage-metrics.json")
        return {"written": True, "local": True}

    with patch.object(lm, "_get_db_secret", side_effect=_local_db_secret), \
         patch.object(lm, "write_to_r2", side_effect=_fake_write):
        log.info("─── Running metrics lambda ───")
        result = lm.lambda_handler({}, {})
        body = json.loads(result.get("body", "{}"))
        log.info(
            "Metrics done — subscribers=%s  campaigns=%s  data_as_of=%s",
            body.get("total_subscribers"),
            body.get("total_campaigns"),
            body.get("data_as_of"),
        )


def run_comparison_lambda():
    import superage_comparison_lambda as cl

    def _fake_write(content: str):
        _write_local(content, "superage-comparison.json")
        return {"written": True, "local": True}

    with patch.object(cl, "_get_db_secret", side_effect=_local_db_secret), \
         patch.object(cl, "write_to_r2", side_effect=_fake_write):
        log.info("─── Running comparison lambda ───")
        cl.lambda_handler({}, {})
        log.info("Comparison done.")


def run_ads_shopify_lambda():
    """One lambda builds BOTH the ads and shopify sections and writes two
    files: superage-ads.json and superage-shopify.json. We route each R2 key
    to the matching local file by inspecting the `key` argument."""
    import superage_ads_shopify_lambda as al

    def _fake_write(content: str, key: str = None):
        key = key or al.R2_FILE_PATH
        filename = "superage-shopify.json" if "shopify" in key else "superage-ads.json"
        _write_local(content, filename)
        return {"written": True, "local": True, "key": key}

    with patch.object(al, "_get_db_secret", side_effect=_local_db_secret), \
         patch.object(al, "write_to_r2", side_effect=_fake_write):
        log.info("─── Running ads + shopify lambda ───")
        result = al.lambda_handler({}, {})
        body = json.loads(result.get("body", "{}"))
        log.info(
            "Ads done — spend=%s  conversions=%s  campaigns=%s",
            body.get("spend"),
            body.get("conversions"),
            body.get("total_campaigns"),
        )
        log.info(
            "Shopify done — orders=%s  net_sales=%s  revenue=%s",
            body.get("shopify_orders"),
            body.get("shopify_net_sales"),
            body.get("shopify_revenue"),
        )


if __name__ == "__main__":
    run_metrics_lambda()
    run_comparison_lambda()
    run_ads_shopify_lambda()

    print("\n✓ All lambdas complete.")
    print("Start a local server and open the dashboard:")
    print("  python -m http.server 8080")
    print("  http://localhost:8080/index.html            (live sections)")
    print("  http://localhost:8080/index.shopify.html    (preview: + Shopify Sales)")
