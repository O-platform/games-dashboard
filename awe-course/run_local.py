"""
Local runner for the AWE Course dashboard lambdas.

Runs awe_metrics_lambda with DB credentials from environment variables (no AWS
Secrets Manager / no R2) and writes awe_course.json next to index.html so the
dashboard can be previewed locally.

Optionally also runs the Campaign Monitor waitlist ingest first (if CM_* env
vars are set) so the local awe_waitlist table is fresh.

Usage
-----
    # DB (required)
    export DB_HOST=powerbi.ctqeq4e88wx8.us-west-1.rds.amazonaws.com
    export DB_PORT=5432
    export DB_NAME=postgres
    export DB_USER=postgres
    export DB_PASSWORD=...
    export SA_SCHEMA=superage
    export DB_SSLMODE=require

    # AWE link identifier (optional — this is the default)
    export AWE_URL_PATTERNS='%superage.com/awecourse%'

    # Campaign Monitor ingest (optional — set INGEST=1 to run it)
    export INGEST=1
    export CM_API_KEY=...
    export CM_CLIENT_ID=...
    export CM_LIST_ID=...

    cd awe-course/
    python run_local.py

    # then serve and open:
    python -m http.server 8080
    # http://localhost:8080/index.html
"""

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger(__name__)

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "lambda"))

# ── env defaults ──
os.environ.setdefault("WRITE_TO_R2", "false")
os.environ.setdefault("SA_SCHEMA", "superage")
os.environ.setdefault("DB_SSLMODE", "require")
os.environ.setdefault("AWE_URL_PATTERNS", "%superage.com/awecourse%")

# ── DB credentials (override via real env vars; do not commit real secrets) ──
os.environ.setdefault("DB_HOST",     "powerbi.ctqeq4e88wx8.us-west-1.rds.amazonaws.com")
os.environ.setdefault("DB_PORT",     "5432")
os.environ.setdefault("DB_NAME",     "postgres")
os.environ.setdefault("DB_USER",     "postgres")
os.environ.setdefault("DB_PASSWORD", "PostgresAdmin1234")


def _local_db_secret():
    for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            raise SystemExit(f"Missing required env var: {k}")
    return {
        "host":     os.environ["DB_HOST"],
        "port":     int(os.environ.get("DB_PORT", "5432")),
        "dbname":   os.environ["DB_NAME"],
        "username": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def run_ingest():
    import awe_waitlist_ingest_lambda as ing
    with patch.object(ing, "_get_db_secret", side_effect=_local_db_secret):
        log.info("─── Running waitlist ingest ───")
        result = ing.lambda_handler({}, None)
        log.info("Ingest result: %s", result)


def run_metrics():
    import awe_metrics_lambda as met

    def _fake_write(content: str):
        out = HERE / "awe_course.json"
        out.write_text(content, encoding="utf-8")
        log.info("Wrote %s (%.1f KB)", out.name, len(content) / 1024)
        return {"uploaded": True, "local": True}

    with patch.object(met, "_get_db_secret", side_effect=_local_db_secret), \
         patch.object(met, "write_to_r2", side_effect=_fake_write):
        log.info("─── Running metrics lambda ───")
        result = met.lambda_handler({}, None)
        body = json.loads(result.get("body", "{}"))
        k = body.get("kpis", {})
        log.info("Metrics done — clickers=%s waitlist=%s clicks=%s",
                 k.get("distinct_clickers"), k.get("waitlist_total"), k.get("total_clicks"))


def use_sample():
    """No DB handy? Copy the sample payload so the HTML has something to render."""
    src = HERE / "awe_course.sample.json"
    dst = HERE / "awe_course.json"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    log.info("USE_SAMPLE=1 -> copied %s to %s", src.name, dst.name)


if __name__ == "__main__":
    if os.environ.get("USE_SAMPLE", "").strip().lower() in {"1", "true", "yes"}:
        # Offline path: generate awe_course.json from the bundled sample (no DB).
        use_sample()
    else:
        # Real path: run the metrics lambda against RDS (and optionally the CM
        # ingest first), writing awe_course.json the dashboard reads on localhost.
        if os.environ.get("INGEST", "").strip().lower() in {"1", "true", "yes"}:
            run_ingest()
        run_metrics()
    print("\n[OK] Done. Serve and open the dashboard:")
    print("  python -m http.server 8080")
    print("  http://localhost:8080/index.html")
