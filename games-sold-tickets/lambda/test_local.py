"""
Local test runner for sales_metrics_lambda.py
----------------------------------------------
pip install psycopg2-binary
python test_local.py
"""

import json, os, sys

# ── Credentials ────────────────────────────────────────────────────────────────
SA_HOST     = "powerbi.ctqeq4e88wx8.us-west-1.rds.amazonaws.com"
SA_PORT     = 5432
SA_DBNAME   = "postgres"
SA_USER     = "postgres"
SA_PASSWORD = "PostgresAdmin1234"
SA_SSLMODE  = "require"

AH_HOST     = "airtable.ctqeq4e88wx8.us-west-1.rds.amazonaws.com"
AH_PORT     = 5432
AH_DBNAME   = "postgres"
AH_USER     = "postgres"
AH_PASSWORD = "postgres123"
AH_SSLMODE  = "require"

# Ageist + HealthBrief share the SA connection
# ───────────────────────────────────────────────────────────────────────────────

os.environ["WRITE_TO_R2"]   = "false"
# These ARNs are never called — we inject the secret cache directly below
# so Secrets Manager is never contacted.
os.environ["DB_SECRET_ARN"] = "arn:aws:secretsmanager:us-west-1:550130133458:secret:prod/rds/postgres-audit-FLCsOT"
os.environ["R2_SECRET_ARN"] = "local-bypass-not-called"

# AH connection reads directly from env vars
os.environ["AH_DB_HOST"]     = AH_HOST
os.environ["AH_DB_PORT"]     = str(AH_PORT)
os.environ["AH_DB_NAME"]     = AH_DBNAME
os.environ["AH_DB_USER"]     = AH_USER
os.environ["AH_DB_PASSWORD"] = AH_PASSWORD
os.environ["AH_DB_SSLMODE"]  = AH_SSLMODE

import unittest.mock as mock
fake_boto3 = mock.MagicMock()

with mock.patch("boto3.client", return_value=fake_boto3):
    import sales_metrics_lambda as lam

# Windows: %-d in strftime is Linux-only. Subclass datetime and replace the
# lambda's reference so .now() and .astimezone() both return patched instances.
import platform, datetime as _dt_mod
if platform.system() == "Windows":
    class _WinDT(_dt_mod.datetime):
        def strftime(self, fmt):
            return super().strftime(fmt.replace("%-d", str(self.day)).replace("%-m", str(self.month)))
        @classmethod
        def now(cls, tz=None):
            t = _dt_mod.datetime.now(tz)
            return cls(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond, t.tzinfo)
        def astimezone(self, tz=None):
            t = _dt_mod.datetime.astimezone(self, tz)
            return _WinDT(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond, t.tzinfo)
    lam.datetime = _WinDT

# Inject SA secret cache directly — bypasses Secrets Manager
lam._db_secret_cache = {
    "host":     SA_HOST,
    "port":     SA_PORT,
    "dbname":   SA_DBNAME,
    "username": SA_USER,
    "password": SA_PASSWORD,
}

# ── Run ────────────────────────────────────────────────────────────────────────
with mock.patch("boto3.client", return_value=fake_boto3):
    result = lam.lambda_handler({}, None)

body = json.loads(result["body"])
M    = lam.json.loads(lam.json.dumps(  # re-hydrate the full payload from the last run
    body, default=str
))

print(f"\nStatus: {result['statusCode']}")
print(f"Data as of: {body.get('data_as_of', '?')}")

print(f"\n{'═'*55}")
print(f"  TICKETS & WAITLIST")
print(f"{'═'*55}")
print(f"  Total tickets    : {body.get('total_tickets', 0):,}")
print(f"  Total waitlist   : {body.get('total_waitlist', 0):,}")
print(f"  Landing events   : {body.get('landing_events', 0):,}")
print(f"  Our brands       : {body.get('landing_our_brands', 0):,}")
print(f"  Sponsors         : {body.get('landing_sponsors', 0):,}")
print(f"  Event partners   : {body.get('landing_events_partners', 0):,}")

src = body.get("_source_totals", {})
print(f"\n{'─'*55}")
print(f"  SOURCE BREAKDOWN")
print(f"{'─'*55}")
print(f"  SA raw email     : {src.get('sa_raw_email', 0):,}")
print(f"  SA website       : {src.get('sa_website_from_landing', 0):,}")
print(f"  AH raw email     : {src.get('ah_raw_email', 0):,}")
print(f"  Ageist raw email : {src.get('ag_raw_email', 0):,}")
print(f"  HB raw email     : {src.get('hb_raw_email', 0):,}")
print(f"  Filtered landing : {src.get('filtered_landing_total', 0):,}")
print(f"  Check total      : {src.get('check_total', 0):,}")

# Write the full JSON so the frontend can load it
out_path = os.path.join(os.path.dirname(__file__), "..", "sales_metrics.json")

# The lambda writes to R2; locally we re-run the queries and capture the payload
# by reading the in-memory object the lambda built (accessible via the return body)
# For full payload we need to re-run — the lambda only returns a summary in body.
# Re-run with direct capture:
import importlib, copy
from unittest.mock import patch

with patch("boto3.client", return_value=fake_boto3):
    # Monkey-patch write_to_r2 to capture the payload instead
    captured = {}
    _orig_write = lam.write_to_r2
    def _capture(content):
        captured["json"] = content
        return {"uploaded": False, "reason": "captured_locally"}
    lam.write_to_r2 = _capture
    lam.WRITE_TO_R2 = False
    lam.lambda_handler({}, None)
    lam.write_to_r2 = _orig_write

if "json" in captured:
    with open(out_path, "w") as f:
        f.write(captured["json"])
    print(f"\nWrote: {os.path.abspath(out_path)}")
else:
    print("\nWarning: could not capture full payload", file=sys.stderr)
