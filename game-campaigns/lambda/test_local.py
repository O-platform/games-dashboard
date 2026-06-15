"""
Local test runner for campaigns_life_lambda.py
-----------------------------------------------
pip install psycopg2-binary
python test_local.py
"""

import json, os, sys
from datetime import datetime, timezone

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
# These ARNs are never called — we inject the secret caches directly below
# so Secrets Manager is never contacted. The values just satisfy the env-var
# checks at import time.
os.environ["DB_SECRET_ARN"] = "arn:aws:secretsmanager:us-west-1:550130133458:secret:prod/rds/postgres-audit-FLCsOT"
os.environ["R2_SECRET_ARN"] = "local-bypass-not-called"

import unittest.mock as mock
fake_boto3 = mock.MagicMock()

with mock.patch("boto3.client", return_value=fake_boto3):
    import campaigns_life_lambda as lam

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

# Inject secrets caches directly — no Secrets Manager call needed
_sa_secret = {"host": SA_HOST, "port": SA_PORT, "dbname": SA_DBNAME, "username": SA_USER, "password": SA_PASSWORD}
_ah_secret = {"host": AH_HOST, "port": AH_PORT, "dbname": AH_DBNAME, "username": AH_USER, "password": AH_PASSWORD}

lam._sa_secret_cache     = _sa_secret
lam._ah_secret_cache     = _ah_secret
lam._ageist_secret_cache = _sa_secret   # same DB

# Patch AH connection to use AH creds directly
import psycopg2
def _ah_conn():
    return psycopg2.connect(host=AH_HOST, port=AH_PORT, dbname=AH_DBNAME,
                             user=AH_USER, password=AH_PASSWORD, sslmode=AH_SSLMODE,
                             connect_timeout=30)
lam.ah_connection = _ah_conn

# ── Run ────────────────────────────────────────────────────────────────────────
try:
    import zoneinfo
    last_updated = datetime.now(timezone.utc).astimezone(
        zoneinfo.ZoneInfo("America/New_York")
    ).strftime("%b %-d, %Y %I:%M %p EST")
except Exception:
    last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

output = {"last_updated": last_updated}

def summarize(key, data):
    k = data.get("kpis", {})
    camps = data.get("campaigns", [])
    print(f"\n{'─'*50}")
    print(f"  {key.upper()}")
    print(f"{'─'*50}")
    print(f"  Campaigns      : {k.get('total_campaigns', 0)}")
    print(f"  Recipients     : {k.get('total_recipients_fmt', '0')}")
    print(f"  Total Opens    : {k.get('total_unique_opens_fmt', '0')}")
    print(f"  Games Clicks   : {k.get('total_game_clicks_fmt', '0')}")
    print(f"  Unique Clickers: {k.get('total_game_unique_fmt', '0')}")
    print(f"  Avg Open Rate  : {k.get('avg_open_rate', '0.00%')}")
    print(f"  Games CTR      : {k.get('game_ctr', '0.00%')}")
    if camps:
        print(f"  Latest 3 campaigns:")
        for c in camps[:3]:
            print(f"    {c.get('sent_date','?')}  {c.get('name','')[:50]}")
            print(f"      Opens: {c.get('unique_opens_fmt','0')}  "
                  f"Games clicks: {c.get('game_clicks_fmt','0')}  "
                  f"Unique: {c.get('game_unique_fmt','0')}  "
                  f"CTR: {c.get('game_ctr','0.00%')}")

for key, fn in [
    ("superage",    lam.query_superage),
    ("allhealthy",  lam.query_allhealthy),
    ("ageist",      lam.query_ageist),
    ("healthbrief", lam.query_healthbrief),
]:
    try:
        data = fn()
        output[key] = data
        summarize(key, data)
    except Exception as e:
        output.setdefault("errors", []).append({"source": key, "error": str(e)})
        print(f"\n  {key.upper()} FAILED: {e}", file=sys.stderr)

out_path = os.path.join(os.path.dirname(__file__), "..", "campaigns_life.json")
with open(out_path, "w") as f:
    json.dump(output, f, indent=2, default=str)

print(f"\nWrote {os.path.abspath(out_path)}")
