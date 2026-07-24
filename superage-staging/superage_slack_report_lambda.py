import os
import gzip
import json
import logging
import re
import base64
from datetime import datetime, timedelta, date, time
from urllib.parse import urlparse, urlunparse, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import boto3
import psycopg2


LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

CM_BASE = os.getenv("CM_BASE", "https://api.createsend.com/api/v3.3").rstrip("/")

REGION_NAME = os.getenv("REGION_NAME", "us-west-1")

DB_SECRET_ID = os.getenv(
    "DB_SECRET_ID",
    "arn:aws:secretsmanager:us-west-1:550130133458:secret:DBMetrics_Superage_Secrets-fnwNnN",
)

ENABLE_FLOWS = os.getenv("ENABLE_FLOWS", "true").lower() == "true"

LAMBDA_TIME_RESERVE_MS = int(os.getenv("LAMBDA_TIME_RESERVE_MS", "60000"))
CM_PAGE_SIZE = int(os.getenv("CM_PAGE_SIZE", "1000"))

SUBSCRIBERS_TABLE_DEFAULT = 'superage."subscribers"'

secrets_client = boto3.client("secretsmanager", region_name=REGION_NAME)


# ============================================================
# Sponsor patterns — first choice for URL matching.
# Each entry: (sponsor_name, [url_substrings])
# If the sponsor name in the issue matches an entry here,
# only URLs containing one of the substrings are counted.
# SPONSOR_DOMAINS_JSON, token matching, and generic fallback
# are only used when NO entry matches here.
# ============================================================

DEFAULT_SPONSOR_PATTERNS = [
    ("OneSkin affiliate", ["oneskin.pxf.io"]),
    ("OneSkin", ["oneskin.co", "oneskin"]),
    ("Acorn Biolabs", ["acorn.me", "acornbiolabs"]),
    ("Apollo", ["apolloneuro", "apollo"]),
    ("Aramore", ["aramore"]),
    ("Athletic Greens (AG-1)", ["drinkag1", "ag1"]),
    ("Beekeeper's Naturals", ["beekeepersnaturals", "beekepersnaturals", "beekeepers"]),
    ("Berkeley Life", ["berkeleylife"]),
    ("BetterHelp - Atwave", ["betterhelp", "rewardcellar"]),
    ("BTL", ["bodybybtl", "exomind", "btl"]),
    ("David Protein", ["davidprotein"]),
    ("Eetho Brands, Inc.", ["eetho"]),
    ("Fatty15", ["fatty15"]),
    ("Fisher Investments -- Atwave", ["fisherinvestments", "pembletonfinancial"]),
    ("Forkful", ["forkful"]),
    ("Geviti", ["geviti"]),
    ("Hear.com", ["hear.com", "hear"]),
    ("Inside Tracker", ["insidetracker"]),
    ("Kinsyn", ["kinsyn"]),
    ("Living Alchemy", ["livingalchemy"]),
    ("LMNT", ["drinklmnt", "lmnt"]),
    ("Maui Nui", ["mauinui"]),
    ("Mimio Health", ["mimiohealth"]),
    ("MOSH", ["moshlife", "mosh"]),
    ("NativePath", ["nativepath", "native path"]),
    ("Noom", ["noom"]),
    ("Oricle", ["getoricle", "oricle"]),
    ("Our Place", ["fromourplace", "ourplace"]),
    ("Ozlo", ["ozlo"]),
    ("Pendulum", ["pendulumlife", "pendulum"]),
    ("Planted", ["planted"]),
    ("Plated", ["platedskinscience", "plated"]),
    ("Puori", ["puori", "pouri"]),
    ("Prolon", ["prolonlife", "prolon"]),
    ("Pvolve", ["pvolve"]),
    ("Shawn Chavez", ["shawnchavez"]),
    ("Spring Sleep", ["springsleep"]),
    ("TimeLine", ["timeline"]),
    ("Troscriptions", ["troscriptions"]),
    ("AG1", ["drinkag1", "ag1"]),
    ("Timeline", ["timeline"]),
]

# Words to exclude when falling back to token matching.
# Prevents false matches on common English words inside article URLs.
COMMON_SPONSOR_TOKEN_STOPWORDS = {
    "a", "an", "and", "at", "by", "co", "com", "for", "from", "get",
    "health", "inc", "life", "of", "our", "place", "the", "to", "with",
}

# Pre-build a lowercase lookup: sponsor_name_lower -> [substrings]
_SPONSOR_PATTERN_MAP = {
    name.lower().strip(): substrings
    for name, substrings in DEFAULT_SPONSOR_PATTERNS
}


# ============================================================
# Helpers
# ============================================================

class TimeBudgetExceeded(Exception):
    pass


def check_time_budget(context, label: str = ""):
    if context is None:
        return

    remaining_ms = context.get_remaining_time_in_millis()

    if remaining_ms < LAMBDA_TIME_RESERVE_MS:
        raise TimeBudgetExceeded(
            f"Stopping before Lambda timeout at {label}. "
            f"Remaining={remaining_ms}ms, reserve={LAMBDA_TIME_RESERVE_MS}ms"
        )


def opt_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def must_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def pct(n: float, d: float) -> float:
    return (100.0 * n / d) if d else 0.0


def fmt_m(n: int) -> str:
    if n >= 1_000_000:
        s = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{s}M"
    return f"{n:,}"


def delta_phrase(metric_label: str, delta: float, decimals: int = 1) -> str:
    if abs(delta) < 10 ** (-(decimals + 1)):
        return f"{metric_label} were flat vs last week"

    direction = "increased" if delta > 0 else "decreased"
    return f"{metric_label} {direction} by {abs(delta):.{decimals}f}% from last week"


def format_week_label(start_day: date, end_day: date) -> str:
    start_label = start_day.strftime("%b %d").replace(" 0", " ")
    end_label = end_day.strftime("%b %d, %Y").replace(" 0", " ")
    return f"Week of {start_label}–{end_label}"


def compute_window_days():
    window_days = int(opt_env("WINDOW_DAYS", "7"))
    if window_days < 1:
        window_days = 7

    # Allow overriding the end date for backfills.
    # Set END_DATE=2026-05-08 in Lambda env vars to replay a specific Friday.
    # Remove END_DATE after the backfill so scheduled runs work normally.
    end_date_override = opt_env("END_DATE", "").strip()
    if end_date_override:
        end_day = date.fromisoformat(end_date_override)
        LOG.info("END_DATE override active: end_day=%s", end_day)
    else:
        end_day = datetime.utcnow().date()

    start_day = end_day - timedelta(days=(window_days - 1))

    return start_day, end_day


def prev_window(start_day: date, end_day: date):
    window_days = (end_day - start_day).days + 1
    prev_end = start_day - timedelta(days=1)
    prev_start = prev_end - timedelta(days=(window_days - 1))

    return prev_start, prev_end


# ============================================================
# Slack
# ============================================================

def _slack_post_to(webhook: str, text: str, label: str = ""):
    """Internal helper — posts to any Slack webhook URL."""
    if not webhook:
        LOG.info("Webhook%s not set; skipping Slack post.", f" ({label})" if label else "")
        return

    payload = {"text": text}

    req = Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                raise RuntimeError(f"Slack webhook HTTP {resp.status}: {body}")
            LOG.info("Posted to Slack%s status=%s", f" ({label})" if label else "", resp.status)

    except HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        LOG.error("Slack HTTPError%s %s: %s", f" ({label})" if label else "", getattr(e, "code", "unknown"), msg)

    except URLError as e:
        LOG.error("Slack URLError%s: %s", f" ({label})" if label else "", e)

    except Exception as e:
        LOG.error("Slack post failed%s: %s", f" ({label})" if label else "", e)


def slack_post(text: str):
    """Main report webhook."""
    webhook = opt_env("SLACK_WEBHOOK_URL", "").strip()
    _slack_post_to(webhook, text, label="main")


def slack_post_mahmoud(text: str):
    """
    Alert webhook for Mahmoud.
    Fires when a sponsor issue could not be matched via DEFAULT_SPONSOR_PATTERNS
    or SPONSOR_DOMAINS_JSON and fell back to token matching or generic fallback.
    Used on Thursday preview runs to catch new/unknown sponsors before Friday production.
    """
    webhook = opt_env("SLACK_WEBHOOK_URL_MAHMOUD", "").strip()
    _slack_post_to(webhook, text, label="mahmoud")


# ============================================================
# Postgres - RDS credentials ONLY from Secrets Manager
# ============================================================

def get_secret(secret_id: str) -> dict:
    LOG.info("Loading DB secret from Secrets Manager: %s", secret_id)

    response = secrets_client.get_secret_value(SecretId=secret_id)

    if "SecretString" in response:
        return json.loads(response["SecretString"])

    secret_binary = response.get("SecretBinary")

    if secret_binary:
        return json.loads(secret_binary.decode("utf-8"))

    raise ValueError(f"Secret {secret_id} does not contain SecretString or SecretBinary")


def get_db_config_from_secret() -> dict:
    secret = get_secret(DB_SECRET_ID)

    db_config = {
        "host": secret.get("host") or secret.get("DB_HOST"),
        "port": int(secret.get("port") or secret.get("DB_PORT") or 5432),
        "dbname": secret.get("dbname") or secret.get("database") or secret.get("DB_NAME") or "postgres",
        "user": secret.get("username") or secret.get("user") or secret.get("DB_USER"),
        "password": secret.get("password") or secret.get("DB_PASSWORD"),
    }

    missing = [
        key for key in ["host", "dbname", "user", "password"]
        if not db_config.get(key)
    ]

    if missing:
        raise ValueError(f"Missing required DB secret fields: {', '.join(missing)}")

    LOG.info(
        "Connecting to DB from secret. host=%s port=%s dbname=%s user=%s",
        db_config["host"],
        db_config["port"],
        db_config["dbname"],
        db_config["user"],
    )

    return db_config


def pg_connect():
    db_config = get_db_config_from_secret()

    return psycopg2.connect(
        host=db_config["host"],
        port=db_config["port"],
        dbname=db_config["dbname"],
        user=db_config["user"],
        password=db_config["password"],
        sslmode=opt_env("RDS_SSLMODE", "require"),
        connect_timeout=10,
    )


# ============================================================
# Filters
# ============================================================

def campaigns_filter_sql() -> str:
    return (
        '("Campaign Name" ILIKE \'The Mindset%%\' '
        'OR "Campaign Name" ILIKE \'Sunday Spotlight%%\' '
        'OR "Campaign Name" ILIKE \'Dedicated%%\' '
        'OR "Campaign Name" ILIKE \'SA_PF_%%\' '
        'OR "Campaign Name" ILIKE \'SA_EA_%%\' '
        'OR "Campaign Name" ILIKE \'SA_CR_%%\')'
    )


def clicks_filter_sql() -> str:
    return (
        '("issue_name" ILIKE \'The Mindset%%\' '
        'OR "issue_name" ILIKE \'Sunday Spotlight%%\' '
        'OR "issue_name" ILIKE \'Dedicated%%\' '
        'OR "issue_name" ILIKE \'SA_PF_%%\' '
        'OR "issue_name" ILIKE \'SA_EA_%%\' '
        'OR "issue_name" ILIKE \'SA_CR_%%\')'
    )


# ============================================================
# URL helpers
# ============================================================

def normalize_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.replace("www.", "")
    except Exception:
        return ""


def is_games_url(url: str) -> bool:
    host = normalize_host(url)
    url_lower = (url or "").lower()

    return (
        host == "games.superage.com"
        or "games.superage.com" in url_lower
        or "/games" in url_lower
    )


def is_excluded_from_sponsor_fallback(url: str) -> bool:
    host = normalize_host(url)
    url_lower = (url or "").lower()

    if not host:
        return True

    excluded_hosts = {
        "superage.com",
        "games.superage.com",
        "amzn.to",
        "amazon.com",
        "a.co",
        "createsend.com",
    }

    if host in excluded_hosts:
        return True

    if host.endswith(".superage.com"):
        return True

    if host.endswith(".amazon.com"):
        return True

    if "superage.com" in url_lower:
        return True

    if "games.superage.com" in url_lower:
        return True

    if "amzn.to" in url_lower or "amazon." in url_lower:
        return True

    return False


def strip_query(url: str) -> str:
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return url


def slug_to_title(path: str) -> str:
    s = (path or "").strip("/")

    if not s:
        return ""

    segment = s.split("/")[-1]
    segment = re.sub(r"\.html?$", "", segment, flags=re.I)
    segment = segment.replace("_", "-")

    words = [word for word in segment.split("-") if word]

    if not words:
        return ""

    return " ".join(word.capitalize() for word in words)


def extract_article_title_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        title = slug_to_title(parsed.path)
        return title or strip_query(url)
    except Exception:
        return url


# ============================================================
# Sponsor parsing & matching
# ============================================================

SPONSOR_RX = re.compile(r"\(Sponsor:\s*([^)]+)\)", re.I)


def find_sponsor_patterns(sponsor_name: str):
    """
    Look up sponsor_name in DEFAULT_SPONSOR_PATTERNS (case-insensitive).
    Returns list of URL substrings to match, or None if not found.
    """
    return _SPONSOR_PATTERN_MAP.get((sponsor_name or "").lower().strip())


def extract_sponsor_name(issue_name: str) -> str:
    if not issue_name:
        return ""

    match = SPONSOR_RX.search(issue_name)

    if not match:
        return ""

    return match.group(1).strip()


def sponsor_tokens(sponsor_name: str):
    """
    Split sponsor name into tokens for fallback substring matching.
    Filters out stopwords to avoid false matches on common words.
    """
    if not sponsor_name:
        return []

    s = sponsor_name.lower()
    s = s.replace("&", " ").replace("+", " ").replace("/", " ")
    s = re.sub(r"\s+", " ", s).strip()

    parts = [part.strip() for part in s.split(" ") if part.strip()]

    # Filter stopwords
    filtered = [p for p in parts if p not in COMMON_SPONSOR_TOKEN_STOPWORDS]

    tokens = list(dict.fromkeys(filtered))

    if len(filtered) >= 2:
        tokens.append(" ".join(filtered))

    return tokens


def pretty_day(issue_date: str) -> str:
    try:
        d = datetime.fromisoformat(issue_date).date()
        return d.strftime("%b %d").replace(" 0", " ")
    except Exception:
        return issue_date


# ============================================================
# DB reads
# ============================================================

def fetch_campaigns_totals_and_issues(conn, campaigns_table: str, start_day: date, end_day: date):
    end_excl = end_day + timedelta(days=1)

    query = f"""
        SELECT
            "Campaign Name",
            "Sent Date ",
            "Recipients",
            "UniqueOpened",
            "Unsubscribed",
            "Bounced"
        FROM {campaigns_table}
        WHERE "Sent Date " >= %s
          AND "Sent Date " < %s
          AND {campaigns_filter_sql()}
        ORDER BY "Sent Date " ASC
    """

    with conn.cursor() as cur:
        cur.execute(query, (start_day, end_excl))
        rows = cur.fetchall()

    issues = []
    totals = {
        "sent": 0,
        "delivered": 0,
        "unique_opened": 0,
        "unsubs": 0,
        "campaigns": 0,
    }

    for name, sent_dt, recipients, unique_opened, unsubs, bounced in rows:
        sent_i = int(recipients or 0)
        bounced_i = int(bounced or 0)
        delivered = max(sent_i - bounced_i, 0)

        totals["sent"] += sent_i
        totals["delivered"] += delivered
        totals["unique_opened"] += int(unique_opened or 0)
        totals["unsubs"] += int(unsubs or 0)
        totals["campaigns"] += 1

        issues.append(
            {
                "issue_name": name,
                "issue_date": sent_dt.date().isoformat() if hasattr(sent_dt, "date") else str(sent_dt)[:10],
                "sponsor_name": extract_sponsor_name(name),
            }
        )

    return totals, issues


def fetch_click_rows(conn, clicks_table: str, start_day: date, end_day: date):
    end_excl = end_day + timedelta(days=1)

    query_trailing_space = f"""
        SELECT "EmailAddress ", "URL", "issue_name"
        FROM {clicks_table}
        WHERE "issue_date" >= %s
          AND "issue_date" < %s
          AND {clicks_filter_sql()}
    """

    query_no_space = f"""
        SELECT "EmailAddress", "URL", "issue_name"
        FROM {clicks_table}
        WHERE "issue_date" >= %s
          AND "issue_date" < %s
          AND {clicks_filter_sql()}
    """

    try:
        with conn.cursor() as cur:
            cur.execute(query_trailing_space, (start_day, end_excl))
            return cur.fetchall()

    except psycopg2.errors.UndefinedColumn:
        conn.rollback()
        LOG.warning('Column "EmailAddress " not found. Retrying with "EmailAddress".')

        with conn.cursor() as cur:
            cur.execute(query_no_space, (start_day, end_excl))
            return cur.fetchall()


def fetch_unique_clickers_count(conn, clicks_table: str, start_day: date, end_day: date):
    end_excl = end_day + timedelta(days=1)

    query_trailing_space = f"""
        SELECT COUNT(DISTINCT LOWER(TRIM("EmailAddress "))) AS unique_clickers
        FROM {clicks_table}
        WHERE "issue_date" >= %s
          AND "issue_date" < %s
          AND "EmailAddress " IS NOT NULL
          AND TRIM("EmailAddress ") <> ''
          AND {clicks_filter_sql()}
    """

    query_no_space = f"""
        SELECT COUNT(DISTINCT LOWER(TRIM("EmailAddress"))) AS unique_clickers
        FROM {clicks_table}
        WHERE "issue_date" >= %s
          AND "issue_date" < %s
          AND "EmailAddress" IS NOT NULL
          AND TRIM("EmailAddress") <> ''
          AND {clicks_filter_sql()}
    """

    try:
        with conn.cursor() as cur:
            cur.execute(query_trailing_space, (start_day, end_excl))
            return int(cur.fetchone()[0] or 0)

    except psycopg2.errors.UndefinedColumn:
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute(query_no_space, (start_day, end_excl))
            return int(cur.fetchone()[0] or 0)


def compute_new_subscribers_for_window_from_rds(
    conn,
    subscribers_table: str,
    start_day: date,
    end_day: date,
):
    end_excl = end_day + timedelta(days=1)

    # Derive schema from subscribers_table (e.g. superage."subscribers" -> superage)
    schema = subscribers_table.split(".")[0].strip('"')
    mv_table = f"{schema}.mv_subscriber_acquisition"

    # Source label comes from the mv_subscriber_acquisition materialized view —
    # the single source of truth for the 5-level chain (acquisition_utm_source
    # >> url_variables Meta gate >> sub_source >> source >> utm_source >>
    # 'Organic', Taboola gated to L1). See sql/mv_subscriber_acquisition.sql.
    #
    # The Slack post groups sources into 5 coarse buckets. source_label maps
    # straight onto taboola / meta / organic; everything else is other_brands.
    # 'unknown' (subscriber with NO source signal at all) is distinguished from
    # 'organic' by checking the raw chain inputs the MV still carries — the MV
    # itself collapses the all-blank case into 'Organic'.
    query = f"""
        WITH base AS (
            SELECT DISTINCT ON (LOWER(TRIM(s.email)))
                LOWER(TRIM(s.email))                          AS email,
                mv.source_label                               AS source_label,
                COALESCE(TRIM(mv.acquisition_utm_source), '') AS acq_utm,
                COALESCE(TRIM(mv.sub_source), '')             AS sub_src,
                COALESCE(TRIM(mv.source), '')                 AS src,
                COALESCE(TRIM(mv.utm_source), '')             AS utm_src,
                COALESCE(TRIM(mv.url_variables), '')          AS url_vars
            FROM {subscribers_table} s
            LEFT JOIN {mv_table} mv ON mv.email = LOWER(TRIM(s.email))
            WHERE s.date_joined >= %s
              AND s.date_joined < %s
              AND s.email IS NOT NULL
              AND TRIM(s.email) <> ''
              AND LOWER(TRIM(COALESCE(s.state, ''))) = 'active'
            ORDER BY LOWER(TRIM(s.email)), s.date_joined ASC
        ),
        classified AS (
            SELECT
                email,
                CASE
                    WHEN source_label = 'Taboola' THEN 'taboola'
                    WHEN source_label = 'Meta'    THEN 'meta'
                    WHEN source_label = 'Organic' OR source_label IS NULL THEN
                        CASE
                            WHEN acq_utm = '' AND sub_src = '' AND src = '' AND utm_src = '' AND url_vars = ''
                                THEN 'unknown'
                            ELSE 'organic'
                        END
                    ELSE 'other_brands'
                END AS source_bucket
            FROM base
        )
        SELECT
            COUNT(*)                                                AS new_subscribers,
            COUNT(*) FILTER (WHERE source_bucket = 'taboola')      AS taboola_subscribers,
            COUNT(*) FILTER (WHERE source_bucket = 'meta')         AS meta_subscribers,
            COUNT(*) FILTER (WHERE source_bucket = 'other_brands') AS other_brand_subscribers,
            COUNT(*) FILTER (WHERE source_bucket = 'organic')      AS organic_subscribers,
            COUNT(*) FILTER (WHERE source_bucket = 'unknown')      AS unknown_subscribers
        FROM classified;
    """

    LOG.info(
        "SUBSCRIBERS_RDS: Fetching new subscribers from %s window=%s -> %s",
        subscribers_table,
        start_day,
        end_excl,
    )

    with conn.cursor() as cur:
        cur.execute(query, (start_day, end_excl))
        row = cur.fetchone()

    new_subscribers = int(row[0] or 0)
    taboola_subscribers = int(row[1] or 0)
    meta_subscribers = int(row[2] or 0)
    other_brand_subscribers = int(row[3] or 0)
    organic_subscribers = int(row[4] or 0)
    unknown_subscribers = int(row[5] or 0)

    def safe_pct(value):
        return round((value / new_subscribers) * 100, 2) if new_subscribers else 0

    LOG.info(
        "SUBSCRIBERS_RDS DONE: total=%s taboola=%s meta=%s other_brands=%s organic=%s unknown=%s",
        new_subscribers,
        taboola_subscribers,
        meta_subscribers,
        other_brand_subscribers,
        organic_subscribers,
        unknown_subscribers,
    )

    return {
        "new_subscribers": new_subscribers,

        "taboola_subscribers": taboola_subscribers,
        "taboola_subscribers_pct": safe_pct(taboola_subscribers),

        "meta_subscribers": meta_subscribers,
        "meta_subscribers_pct": safe_pct(meta_subscribers),

        "other_brand_subscribers": other_brand_subscribers,
        "other_brand_subscribers_pct": safe_pct(other_brand_subscribers),

        "organic_subscribers": organic_subscribers,
        "organic_subscribers_pct": safe_pct(organic_subscribers),

        "unknown_subscribers": unknown_subscribers,
        "unknown_subscribers_pct": safe_pct(unknown_subscribers),

        "source": "rds_subscribers",
        "subscribers_table": subscribers_table,
    }


def compute_games_summary_from_rds(conn, campaigns_table: str, clicks_table: str, start_day: date, end_day: date):
    end_excl = end_day + timedelta(days=1)

    campaign_query = f"""
        SELECT
            COUNT(*) AS games_campaigns_sent,
            COALESCE(SUM("Recipients"), 0) AS games_recipients
        FROM {campaigns_table}
        WHERE "Sent Date " >= %s
          AND "Sent Date " < %s
          AND (
                "Campaign Name" ILIKE '%%Super Age Games%%'
                OR "Campaign Name" ILIKE '%%Games%%'
                OR "Campaign Name" ILIKE 'SA_EA_%%'
          )
    """

    clicks_query_trailing_space = f"""
        SELECT
            COUNT(*) AS total_games_clicks,
            COUNT(DISTINCT LOWER(TRIM("EmailAddress "))) AS unique_games_clickers
        FROM {clicks_table}
        WHERE "issue_date" >= %s
          AND "issue_date" < %s
          AND "URL" ILIKE '%%games.superage.com%%'
          AND "EmailAddress " IS NOT NULL
          AND TRIM("EmailAddress ") <> ''
    """

    clicks_query_no_space = f"""
        SELECT
            COUNT(*) AS total_games_clicks,
            COUNT(DISTINCT LOWER(TRIM("EmailAddress"))) AS unique_games_clickers
        FROM {clicks_table}
        WHERE "issue_date" >= %s
          AND "issue_date" < %s
          AND "URL" ILIKE '%%games.superage.com%%'
          AND "EmailAddress" IS NOT NULL
          AND TRIM("EmailAddress") <> ''
    """

    with conn.cursor() as cur:
        cur.execute(campaign_query, (start_day, end_excl))
        campaign_row = cur.fetchone()

    try:
        with conn.cursor() as cur:
            cur.execute(clicks_query_trailing_space, (start_day, end_excl))
            clicks_row = cur.fetchone()

    except psycopg2.errors.UndefinedColumn:
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute(clicks_query_no_space, (start_day, end_excl))
            clicks_row = cur.fetchone()

    games_campaigns_sent = int(campaign_row[0] or 0)
    games_recipients = int(campaign_row[1] or 0)
    total_games_clicks = int(clicks_row[0] or 0)
    unique_games_clickers = int(clicks_row[1] or 0)

    LOG.info(
        "GAMES_RDS DONE: campaigns=%s recipients=%s total_clicks=%s unique_clickers=%s",
        games_campaigns_sent,
        games_recipients,
        total_games_clicks,
        unique_games_clickers,
    )

    return {
        "games_campaigns_sent": games_campaigns_sent,
        "games_recipients": games_recipients,
        "total_games_url_clicks": total_games_clicks,
        "unique_games_url_clickers": unique_games_clickers,
        "source": "rds_campaigns_clicks",
    }


# ============================================================
# Click aggregations
# ============================================================

def aggregate_immersion_clicks(
    click_rows,
    immersions_prefix: str = "https://superage.com/immersions",
    top_n: int = 10,
):
    url_to_emails = {}
    url_to_total = {}

    prefix = (immersions_prefix or "").lower()

    for email, url, issue_name in click_rows:
        if not email or not url:
            continue

        url_lower = str(url).lower()

        if prefix not in url_lower:
            continue

        url_clean = strip_query(url)
        url_to_total[url_clean] = url_to_total.get(url_clean, 0) + 1
        url_to_emails.setdefault(url_clean, set()).add(str(email).strip().lower())

    ranked = sorted(
        ((url, len(emails), url_to_total.get(url, 0)) for url, emails in url_to_emails.items()),
        key=lambda x: (x[1], x[2]),
        reverse=True,
    )[:max(int(top_n or 0), 0)]

    return [
        {
            "title": extract_article_title_from_url(url),
            "url": url,
            "unique_clicks": unique_clicks,
            "total_clicks": total_clicks,
        }
        for url, unique_clicks, total_clicks in ranked
    ]


def aggregate_content_hits(click_rows, sponsor_domains: dict):
    url_to_emails = {}

    for email, url, issue_name in click_rows:
        if not email or not url:
            continue

        if is_games_url(url):
            continue

        host = normalize_host(url)

        if host in (sponsor_domains or {}):
            continue

        if host and host not in ("superage.com", "www.superage.com"):
            continue

        url_clean = strip_query(url)
        url_to_emails.setdefault(url_clean, set()).add(str(email).strip().lower())

    ranked = sorted(
        ((url, len(emails)) for url, emails in url_to_emails.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )[:3]

    return [
        {
            "title": extract_article_title_from_url(url),
            "url": url,
            "unique_clicks": unique_clicks,
        }
        for url, unique_clicks in ranked
    ]


def aggregate_sponsor_clicks_per_issue(click_rows, issues, sponsor_domains: dict):
    """
    Sponsor click matching — priority order:

    CASE 1 — DEFAULT_SPONSOR_PATTERNS (first choice):
             Hardcoded URL substrings per sponsor name. Most precise.

    CASE 2 — SPONSOR_DOMAINS_JSON domain config (second choice):
             Explicit domain mapping in env var. Exact host match only.
             Zero domain hits → report 0, no further fallback.

    CASE 3 — Token matching (third choice):
             Sponsor name split into words, stopwords filtered, substring match.
             Triggers Mahmoud alert.

    CASE 4 — Generic fallback (last resort):
             All non-superage external clicks in the issue.
             Triggers Mahmoud alert.

    Returns:
        sponsor_clicks  dict  — per-issue click summary
        unmatched       list  — sponsors that fell to Case 3 or 4,
                                used to fire the Mahmoud alert
    """

    by_issue = {}
    for email, url, issue_name in click_rows:
        if not issue_name or not email or not url:
            continue
        by_issue.setdefault(issue_name, []).append((str(email).strip().lower(), url))

    label_to_domains = {}
    for domain, label in (sponsor_domains or {}).items():
        labels = label if isinstance(label, list) else [label]
        for lbl in labels:
            label_to_domains.setdefault(lbl.lower().strip(), set()).add(
                domain.lower().replace("www.", "")
            )

    sponsor_clicks = {}
    unmatched = []

    for issue in issues:
        issue_name = issue["issue_name"]
        issue_date = issue.get("issue_date") or ""
        sponsor_name = (issue.get("sponsor_name") or "").strip()

        if not sponsor_name:
            continue

        sponsor_label_norm = sponsor_name.lower().strip()
        rows_for_issue = by_issue.get(issue_name, [])

        matched_emails = set()
        total = 0
        matched_urls = {}
        match_case = None

        pattern_substrings = find_sponsor_patterns(sponsor_name)

        if pattern_substrings is not None:
            match_case = "pattern"
            LOG.info("SPONSOR_MATCH: pattern hit sponsor=%s substrings=%s", sponsor_name, pattern_substrings)
            for email, url in rows_for_issue:
                url_lower = (url or "").lower()
                if any(sub and sub in url_lower for sub in pattern_substrings):
                    matched_emails.add(email)
                    total += 1
                    clean_url = strip_query(url)
                    matched_urls[clean_url] = matched_urls.get(clean_url, 0) + 1

        else:
            has_domain_config = bool(label_to_domains.get(sponsor_label_norm))

            if has_domain_config:
                match_case = "domain"
                LOG.info("SPONSOR_MATCH: domain config hit sponsor=%s domains=%s", sponsor_name, label_to_domains.get(sponsor_label_norm))
                for email, url in rows_for_issue:
                    host = normalize_host(url)
                    if host in label_to_domains.get(sponsor_label_norm, set()):
                        matched_emails.add(email)
                        total += 1
                        clean_url = strip_query(url)
                        matched_urls[clean_url] = matched_urls.get(clean_url, 0) + 1

            else:
                tokens = sponsor_tokens(sponsor_name)
                LOG.info("SPONSOR_MATCH: token fallback sponsor=%s tokens=%s", sponsor_name, tokens)

                for email, url in rows_for_issue:
                    url_lower = (url or "").lower()
                    if any(token and token in url_lower for token in tokens):
                        matched_emails.add(email)
                        total += 1
                        clean_url = strip_query(url)
                        matched_urls[clean_url] = matched_urls.get(clean_url, 0) + 1

                if total == 0:
                    match_case = "generic_fallback"
                    LOG.info("SPONSOR_MATCH: generic fallback sponsor=%s", sponsor_name)
                    for email, url in rows_for_issue:
                        if is_excluded_from_sponsor_fallback(url):
                            continue
                        matched_emails.add(email)
                        total += 1
                        clean_url = strip_query(url)
                        matched_urls[clean_url] = matched_urls.get(clean_url, 0) + 1
                else:
                    match_case = "token"

                unmatched.append({
                    "sponsor": sponsor_name,
                    "issue_name": issue_name,
                    "issue_date": issue_date,
                    "match_case": match_case,
                    "matched_urls": list(matched_urls.keys())[:5],
                })

        sponsor_clicks[issue_name] = {
            "sponsor": sponsor_name,
            "issue_date": issue_date,
            "unique": len(matched_emails),
            "total": total,
            "matched_urls": matched_urls,
            "match_case": match_case,
        }

    return sponsor_clicks, unmatched


def build_mahmoud_alert(unmatched: list, start_day: date, end_day: date) -> str:
    lines = [
        f"⚠️ *Sponsor Match Alert — {format_week_label(start_day, end_day)}*",
        f"The following {len(unmatched)} sponsor(s) were not found in DEFAULT_SPONSOR_PATTERNS "
        f"or SPONSOR_DOMAINS_JSON and used fallback matching. "
        f"Please review and add them to the patterns list before the Friday run.\n",
    ]

    for item in unmatched:
        case_label = "token matching" if item["match_case"] == "token" else "generic fallback (no URL match found)"
        lines.append(f"*{item['sponsor']}* — `{item['issue_name']}`")
        lines.append(f"  Matched via: {case_label}")
        if item["matched_urls"]:
            lines.append(f"  URLs seen: {', '.join(item['matched_urls'])}")
        lines.append("")

    lines.append("To fix: add an entry to `DEFAULT_SPONSOR_PATTERNS` in the Lambda code.")

    return "\n".join(lines)


def unique_clickers(click_rows):
    emails = set()

    for email, url, issue_name in click_rows:
        if email:
            emails.add(str(email).strip().lower())

    return len(emails)


# ============================================================
# Campaign Monitor API helpers
# ============================================================

def cm_basic_auth_header(api_key: str) -> str:
    token = base64.b64encode((api_key + ":").encode("utf-8")).decode("utf-8")
    return f"Basic {token}"


def cm_request_json(method: str, full_url: str, api_key: str, context=None):
    check_time_budget(context, f"CM request {full_url[:120]}")

    req = Request(full_url, method=method.upper())
    req.add_header("Authorization", cm_basic_auth_header(api_key))
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Encoding", "identity")

    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()

            if enc == "gzip" or (len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B):
                raw = gzip.decompress(raw)

            text = raw.decode("utf-8", errors="strict") if raw else ""
            return json.loads(text) if text else None

    except HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""

        try:
            if len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B:
                raw = gzip.decompress(raw)
        except Exception:
            pass

        msg = raw.decode("utf-8", errors="replace") if raw else str(e)
        raise RuntimeError(f"CM HTTPError {getattr(e, 'code', 'unknown')} for {full_url}: {msg}")

    except URLError as e:
        raise RuntimeError(f"CM URLError for {full_url}: {e}")


def parse_cm_dt(value) -> datetime:
    if not value:
        return datetime.min

    s = str(value).strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass

    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return datetime.min


def cm_iter_paged_until_end(
    url_base_with_json: str,
    api_key: str,
    qs: dict,
    end_excl: datetime,
    dt_field: str,
    label: str,
    context=None,
):
    page = 1

    while True:
        check_time_budget(context, f"{label} page={page}")

        q = dict(qs or {})
        q["page"] = page

        url = url_base_with_json + "?" + urlencode(q)

        LOG.info("CM_PAGE: %s page=%s", label, page)

        data = cm_request_json("GET", url, api_key, context=context) or {}
        results = data.get("Results") or []

        total_pages = int(
            data.get("NumberOfPages")
            or data.get("Paging", {}).get("TotalNumberOfPages")
            or 1
        )

        LOG.info(
            "CM_PAGE_RESULT: %s page=%s rows=%s total_pages=%s",
            label,
            page,
            len(results),
            total_pages,
        )

        if not results:
            return

        stop = False

        for row in results:
            event_dt = parse_cm_dt(row.get(dt_field) or "")

            if end_excl and event_dt >= end_excl:
                stop = True
                break

            yield row

        if stop or page >= total_pages:
            return

        page += 1


# ============================================================
# Flows / Journeys
# ============================================================

def flows_keywords():
    default = {
        "Meno Quiz": ["Meno Quiz Results Journey", "Menopause Quiz"],
        "Welcome": [
            "The Mindset Welcome Journey",
            "Welcome journey series",
            "Welcome journey",
        ],
        "Quiz": [
            "Longevity Quiz Completed",
            "Fitness Quiz v1 Completed",
            "Fitness Quiz v2 Completed",
            "Quiz Completed",
        ],
    }

    raw = opt_env("FLOW_NAME_KEYWORDS_JSON", "").strip()

    if not raw:
        return default

    try:
        parsed = json.loads(raw)
        out = {}

        for key, value in parsed.items():
            if isinstance(value, list):
                out[key] = value
            elif isinstance(value, str):
                out[key] = [value]

        return out or default

    except Exception:
        return default


def match_flow_label(journey_name: str, mapping: dict):
    name = (journey_name or "").lower()

    for label, keywords in mapping.items():
        for keyword in keywords:
            if keyword and keyword.lower() in name:
                return label

    return None


def compute_journey_ui_style_metrics(
    journey_id: str,
    journey_name: str,
    api_key: str,
    start_dt: datetime,
    end_excl: datetime,
    context=None,
):
    detail = cm_request_json(
        "GET",
        f"{CM_BASE}/journeys/{journey_id}.json",
        api_key,
        context=context,
    ) or {}

    emails = detail.get("Emails") or []
    email_ids = [email.get("EmailID") for email in emails if email.get("EmailID")]

    LOG.info(
        "FLOWS: journey_id=%s journey_name=%s email_count=%s",
        journey_id,
        journey_name,
        len(email_ids),
    )

    query_params = {
        "date": start_dt.strftime("%Y-%m-%d %H:%M"),
        "pagesize": CM_PAGE_SIZE,
        "orderdirection": "asc",
    }

    sends_key = set()
    recipients_set = set()
    bounced_addr = set()
    opened_addr = set()
    clicked_addr = set()
    unsubbed_addr = set()

    endpoint_specs = [
        ("recipients", "recipient", "SentDate"),
        ("bounces", "bounce", "Date"),
        ("opens", "open", "Date"),
        ("clicks", "click", "Date"),
        ("unsubscribes", "unsubscribe", "Date"),
    ]

    for email_id in email_ids:
        check_time_budget(context, f"journey={journey_name} email={email_id}")

        for path, event_type, dt_field in endpoint_specs:
            label = f"{journey_name} | {email_id} | {event_type}"
            url_base = f"{CM_BASE}/journeys/email/{email_id}/{path}.json"

            LOG.info("FLOWS: Fetching %s", label)

            used_rows = 0

            for row in cm_iter_paged_until_end(
                url_base_with_json=url_base,
                api_key=api_key,
                qs=query_params,
                end_excl=end_excl,
                dt_field=dt_field,
                label=label,
                context=context,
            ):
                email = (row.get("EmailAddress") or "").strip().lower()

                if not email:
                    continue

                if event_type == "recipient":
                    recipients_set.add(email)
                    sends_key.add((email_id, email))

                elif event_type == "bounce":
                    if (email_id, email) not in sends_key:
                        continue
                    bounced_addr.add(email)

                elif event_type == "open":
                    if (email_id, email) not in sends_key:
                        continue
                    opened_addr.add(email)

                elif event_type == "click":
                    if (email_id, email) not in sends_key:
                        continue
                    clicked_addr.add(email)

                elif event_type == "unsubscribe":
                    if (email_id, email) not in sends_key:
                        continue
                    unsubbed_addr.add(email)

                used_rows += 1

            LOG.info("FLOWS: Finished %s rows_used=%s", label, used_rows)

    delivered_recipients = max(len(recipients_set) - len(bounced_addr), 0)

    return {
        "recipients": len(recipients_set),
        "delivered_recipients": delivered_recipients,
        "unique_opened": len(opened_addr),
        "unique_clicked": len(clicked_addr),
        "unique_unsubscribed": len(unsubbed_addr),
        "open_rate_pct": round(pct(len(opened_addr), delivered_recipients), 1),
        "ctr_pct": round(pct(len(clicked_addr), delivered_recipients), 2),
        "unsub_rate_pct": round(pct(len(unsubbed_addr), delivered_recipients), 2),
    }


def combine_flow_metrics(
    existing: dict,
    new_metrics: dict,
    journey_name: str,
    journey_status: str,
    journey_list_id: str,
):
    if not existing:
        existing = {
            "journey_names": [],
            "journey_statuses": [],
            "journey_list_ids": [],
            "recipients": 0,
            "delivered_recipients": 0,
            "unique_opened": 0,
            "unique_clicked": 0,
            "unique_unsubscribed": 0,
            "open_rate_pct": 0,
            "ctr_pct": 0,
            "unsub_rate_pct": 0,
        }

    existing["journey_names"].append(journey_name)
    existing["journey_statuses"].append(journey_status)
    existing["journey_list_ids"].append(journey_list_id)

    existing["recipients"] += int(new_metrics.get("recipients", 0) or 0)
    existing["delivered_recipients"] += int(new_metrics.get("delivered_recipients", 0) or 0)
    existing["unique_opened"] += int(new_metrics.get("unique_opened", 0) or 0)
    existing["unique_clicked"] += int(new_metrics.get("unique_clicked", 0) or 0)
    existing["unique_unsubscribed"] += int(new_metrics.get("unique_unsubscribed", 0) or 0)

    delivered = existing["delivered_recipients"]

    existing["open_rate_pct"] = round(pct(existing["unique_opened"], delivered), 1)
    existing["ctr_pct"] = round(pct(existing["unique_clicked"], delivered), 2)
    existing["unsub_rate_pct"] = round(pct(existing["unique_unsubscribed"], delivered), 2)
    existing["journey_name"] = ", ".join(existing["journey_names"])

    return existing


def compute_flows_for_window(client_id: str, api_key: str, start_day: date, end_day: date, context=None):
    mapping = flows_keywords()

    start_dt = datetime.combine(start_day, time(0, 0))
    end_excl = datetime.combine(end_day + timedelta(days=1), time(0, 0))

    journeys_url = f"{CM_BASE}/clients/{client_id}/journeys.json"

    LOG.info("FLOWS: Fetching journeys for client_id=%s", client_id)
    all_journeys = cm_request_json("GET", journeys_url, api_key, context=context) or []
    LOG.info("FLOWS: Found %s journeys", len(all_journeys))

    flows = {}
    skipped_journeys = []

    for journey in all_journeys:
        check_time_budget(context, "compute_flows_for_window journey loop")

        journey_id = journey.get("JourneyID") or ""
        journey_name = journey.get("Name") or ""
        status = str(journey.get("Status") or "").strip()
        list_id = journey.get("ListID") or ""

        LOG.info(
            "FLOWS: Checking journey name=%s id=%s status=%s list_id=%s",
            journey_name,
            journey_id,
            status,
            list_id,
        )

        if not journey_id:
            continue

        if status.lower() != "active":
            skipped_journeys.append(
                {
                    "journey_name": journey_name,
                    "journey_id": journey_id,
                    "status": status,
                    "reason": "not_active",
                }
            )
            continue

        label = match_flow_label(journey_name, mapping)

        if not label:
            skipped_journeys.append(
                {
                    "journey_name": journey_name,
                    "journey_id": journey_id,
                    "status": status,
                    "reason": "name_not_matched",
                }
            )
            continue

        LOG.info(
            "FLOWS: Matched ACTIVE journey label=%s name=%s id=%s",
            label,
            journey_name,
            journey_id,
        )

        metrics = compute_journey_ui_style_metrics(
            journey_id=journey_id,
            journey_name=journey_name,
            api_key=api_key,
            start_dt=start_dt,
            end_excl=end_excl,
            context=context,
        )

        flows[label] = combine_flow_metrics(
            existing=flows.get(label),
            new_metrics=metrics,
            journey_name=journey_name,
            journey_status=status,
            journey_list_id=list_id,
        )

    return flows, skipped_journeys


# ============================================================
# Report text
# ============================================================

def build_report_text(
    start_day: date,
    end_day: date,
    overall_now: dict,
    overall_prev: dict,
    subscriber_summary: dict,
    games_summary: dict,
    top_content_hits: list,
    sponsor_clicks_by_issue: dict,
    immersion_clicks: list,
    flows: dict,
):
    lines = []

    lines.append(f"*The Mindset — {format_week_label(start_day, end_day)}*")

    open_delta = round(overall_now["open_rate_pct"] - overall_prev["open_rate_pct"], 1)
    click_delta = round(overall_now["click_rate_pct"] - overall_prev["click_rate_pct"], 2)
    unsub_delta = round(overall_now["unsub_rate_pct"] - overall_prev["unsub_rate_pct"], 2)

    lines.append(
        f"Overall ({overall_now['campaigns']} campaigns) "
        f"{fmt_m(overall_now['sent'])} sent · "
        f"{overall_now['open_rate_pct']}% open rate ({open_delta:+.1f}%) · "
        f"{overall_now['click_rate_pct']}% click rate ({click_delta:+.2f}%) · "
        f"{overall_now['unsub_rate_pct']}% unsub"
    )

    lines.append(
        f"({delta_phrase('Opens', open_delta, 1)} · "
        f"{delta_phrase('Clicks', click_delta, 2)} · "
        f"{delta_phrase('Unsubs', unsub_delta, 2)})"
    )

    if subscriber_summary:
        lines.append(
            f"This week we had {subscriber_summary.get('new_subscribers', 0):,} new subscribers "
            f"({subscriber_summary.get('taboola_subscribers', 0):,} Taboola "
            f"[{subscriber_summary.get('taboola_subscribers_pct', 0)}%], "
            f"{subscriber_summary.get('meta_subscribers', 0):,} Meta/Facebook "
            f"[{subscriber_summary.get('meta_subscribers_pct', 0)}%], "
            f"{subscriber_summary.get('other_brand_subscribers', 0):,} other brands "
            f"[{subscriber_summary.get('other_brand_subscribers_pct', 0)}%], "
            f"{subscriber_summary.get('organic_subscribers', 0):,} organic "
            f"[{subscriber_summary.get('organic_subscribers_pct', 0)}%], "
            f"{subscriber_summary.get('unknown_subscribers', 0):,} unknown "
            f"[{subscriber_summary.get('unknown_subscribers_pct', 0)}%])."
        )

    if games_summary and games_summary.get("games_campaigns_sent", 0) > 0:
        lines.append("")
        lines.append("*Games Campaigns*")
        lines.append(
            f"{games_summary.get('games_campaigns_sent', 0):,} games campaigns sent · "
            f"{games_summary.get('total_games_url_clicks', 0):,} total clicks on games URLs · "
            f"{games_summary.get('unique_games_url_clickers', 0):,} unique games clickers"
        )

    if top_content_hits:
        lines.append("")
        lines.append("*Top Content Hits*")

        for item in top_content_hits:
            lines.append(f"\"{item['title']}\" — {item['unique_clicks']:,} unique clicks")

    entries = []

    for _issue_name, value in (sponsor_clicks_by_issue or {}).items():
        unique = value.get("unique") or 0
        total = value.get("total") or 0

        if unique == 0 and total == 0:
            continue

        entries.append(
            (
                value.get("issue_date") or "",
                value.get("sponsor") or "",
                unique,
                total,
            )
        )

    entries.sort(key=lambda x: x[0])

    if entries:
        lines.append("")
        lines.append("*Sponsor Clicks*")

        for issue_date, sponsor, unique, total in entries:
            lines.append(f"{sponsor} ({pretty_day(issue_date)}): {unique:,} unique / {total:,} total")

    if immersion_clicks:
        lines.append("")
        lines.append("*Immersion Clicks*")

        for item in immersion_clicks:
            lines.append(
                f"\"{item['title']}\" — {item['unique_clicks']:,} unique / {item['total_clicks']:,} total"
            )

    if flows:
        lines.append("")
        lines.append("*Flows (Past 7 Days)*")

        order = ["Welcome", "Quiz", "Meno Quiz"]

        for key in order:
            if key in flows:
                flow = flows[key]
                label = "Longevity Quiz" if key == "Quiz" else key
                lines.append(
                    f"{label}: {flow['recipients']:,} recipients · "
                    f"{flow['open_rate_pct']}% open · {flow['ctr_pct']}% CTR"
                )

        for key, flow in flows.items():
            if key in order:
                continue

            lines.append(
                f"{key}: {flow['recipients']:,} recipients · "
                f"{flow['open_rate_pct']}% open · {flow['ctr_pct']}% CTR"
            )

    return "\n".join(lines)


# ============================================================
# Lambda
# ============================================================

def lambda_handler(event, context):
    campaigns_table = opt_env("CAMPAIGNS_TABLE", 'superage."Campaigns"')
    clicks_table = opt_env("CLICKS_TABLE", 'superage."Campaigns_Clicks"')
    subscribers_table = opt_env("SUBSCRIBERS_TABLE", SUBSCRIBERS_TABLE_DEFAULT)

    api_key = must_env("CM_API_KEY")
    client_id = opt_env("CLIENT_ID", "c124bc9f78eabcd2464adb2d149fa98d")

    try:
        sponsor_domains = json.loads(opt_env("SPONSOR_DOMAINS_JSON", "{}")) or {}
    except Exception:
        sponsor_domains = {}

    start_day, end_day = compute_window_days()
    prev_start, prev_end = prev_window(start_day, end_day)

    LOG.info("Window      %s -> %s", start_day, end_day)
    LOG.info("Prev window %s -> %s", prev_start, prev_end)
    LOG.info("DB_SECRET_ID %s", DB_SECRET_ID)

    conn = None

    subscriber_summary = {
        "new_subscribers": 0,
        "taboola_subscribers": 0,
        "taboola_subscribers_pct": 0,
        "meta_subscribers": 0,
        "meta_subscribers_pct": 0,
        "other_brand_subscribers": 0,
        "other_brand_subscribers_pct": 0,
        "organic_subscribers": 0,
        "organic_subscribers_pct": 0,
        "unknown_subscribers": 0,
        "unknown_subscribers_pct": 0,
        "source": "rds_subscribers",
        "subscribers_table": subscribers_table,
    }

    games_summary = {
        "games_campaigns_sent": 0,
        "games_recipients": 0,
        "total_games_url_clicks": 0,
        "unique_games_url_clickers": 0,
        "source": "rds_campaigns_clicks",
    }

    subscribers_error = None
    games_error = None

    try:
        LOG.info("STEP 1: Connecting to RDS...")
        conn = pg_connect()
        LOG.info("STEP 1 DONE: Connected to RDS.")

        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60s';")

        LOG.info("DB statement_timeout set to 60 seconds.")

        LOG.info("STEP 2: Fetching current campaign totals from table=%s", campaigns_table)
        totals_now, issues_now = fetch_campaigns_totals_and_issues(
            conn,
            campaigns_table,
            start_day,
            end_day,
        )
        LOG.info("STEP 2 DONE: Current campaign totals fetched. campaigns=%s", totals_now["campaigns"])

        LOG.info("STEP 3: Fetching current click rows from table=%s", clicks_table)
        click_rows_now = fetch_click_rows(
            conn,
            clicks_table,
            start_day,
            end_day,
        )
        LOG.info("STEP 3 DONE: Current click rows fetched. rows=%s", len(click_rows_now))

        LOG.info("STEP 4: Fetching previous campaign totals.")
        totals_prev, _issues_prev = fetch_campaigns_totals_and_issues(
            conn,
            campaigns_table,
            prev_start,
            prev_end,
        )
        LOG.info("STEP 4 DONE: Previous campaign totals fetched. campaigns=%s", totals_prev["campaigns"])

        LOG.info("STEP 5: Fetching previous unique clickers count.")
        unique_clickers_prev = fetch_unique_clickers_count(
            conn,
            clicks_table,
            prev_start,
            prev_end,
        )
        LOG.info("STEP 5 DONE: Previous unique clickers count=%s", unique_clickers_prev)

        try:
            LOG.info("STEP 6: Computing subscriber summary from RDS table=%s", subscribers_table)
            subscriber_summary = compute_new_subscribers_for_window_from_rds(
                conn=conn,
                subscribers_table=subscribers_table,
                start_day=start_day,
                end_day=end_day,
            )
            LOG.info("STEP 6 DONE: Subscriber summary computed from RDS.")

        except Exception as e:
            LOG.exception("RDS subscriber summary failed: %s", e)
            subscribers_error = str(e)

        try:
            LOG.info("STEP 7: Computing games summary from RDS.")
            games_summary = compute_games_summary_from_rds(
                conn=conn,
                campaigns_table=campaigns_table,
                clicks_table=clicks_table,
                start_day=start_day,
                end_day=end_day,
            )
            LOG.info("STEP 7 DONE: Games summary computed from RDS.")

        except Exception as e:
            LOG.exception("RDS games summary failed: %s", e)
            games_error = str(e)

    finally:
        if conn:
            conn.close()
            LOG.info("RDS connection closed.")

    unique_clickers_now = unique_clickers(click_rows_now)

    overall_now = {
        "campaigns": totals_now["campaigns"],
        "sent": totals_now["sent"],
        "delivered": totals_now["delivered"],
        "open_rate_pct": round(pct(totals_now["unique_opened"], totals_now["delivered"]), 1),
        "unsub_rate_pct": round(pct(totals_now["unsubs"], totals_now["delivered"]), 2),
        "click_rate_pct": round(pct(unique_clickers_now, totals_now["delivered"]), 2),
    }

    overall_prev = {
        "campaigns": totals_prev["campaigns"],
        "sent": totals_prev["sent"],
        "delivered": totals_prev["delivered"],
        "open_rate_pct": round(pct(totals_prev["unique_opened"], totals_prev["delivered"]), 1),
        "unsub_rate_pct": round(pct(totals_prev["unsubs"], totals_prev["delivered"]), 2),
        "click_rate_pct": round(pct(unique_clickers_prev, totals_prev["delivered"]), 2),
    }

    top_content_hits = aggregate_content_hits(click_rows_now, sponsor_domains)

    immersion_clicks = aggregate_immersion_clicks(
        click_rows_now,
        immersions_prefix=opt_env("IMMERSIONS_PREFIX", "https://superage.com/immersions"),
        top_n=int(opt_env("IMMERSIONS_TOP_N", "10")),
    )

    sponsor_clicks_by_issue, unmatched_sponsors = aggregate_sponsor_clicks_per_issue(
        click_rows_now,
        issues_now,
        sponsor_domains,
    )

    if unmatched_sponsors:
        LOG.info("SPONSOR_ALERT: %s unmatched sponsor(s) — sending alert to Mahmoud.", len(unmatched_sponsors))
        alert_text = build_mahmoud_alert(unmatched_sponsors, start_day, end_day)
        slack_post_mahmoud(alert_text)
    else:
        LOG.info("SPONSOR_ALERT: All sponsors matched via patterns or domain config. No alert needed.")

    flows = {}
    skipped_journeys = []
    flows_error = None

    if ENABLE_FLOWS:
        try:
            LOG.info("STEP 8: Computing ACTIVE flows from Campaign Monitor within date range...")
            flows, skipped_journeys = compute_flows_for_window(
                client_id=client_id,
                api_key=api_key,
                start_day=start_day,
                end_day=end_day,
                context=context,
            )
            LOG.info("STEP 8 DONE: Active flows computed.")

        except TimeBudgetExceeded as e:
            LOG.warning("STEP 8 STOPPED BEFORE TIMEOUT: %s", e)
            flows_error = str(e)

        except Exception as e:
            LOG.exception("Flows fetch failed: %s", e)
            flows_error = str(e)
            flows = {}

    else:
        flows_error = "Flows skipped because ENABLE_FLOWS=false"
        LOG.info("STEP 8 SKIPPED: %s", flows_error)

    report_text = build_report_text(
        start_day=start_day,
        end_day=end_day,
        overall_now=overall_now,
        overall_prev=overall_prev,
        subscriber_summary=subscriber_summary,
        games_summary=games_summary,
        top_content_hits=top_content_hits,
        sponsor_clicks_by_issue=sponsor_clicks_by_issue,
        immersion_clicks=immersion_clicks,
        flows=flows,
    )

    slack_post(report_text)

    return {
        "ok": True,
        "data": {
            "brand": "Mindset",
            "client_id": client_id,
            "db_secret_id": DB_SECRET_ID,
            "window": {
                "start": start_day.isoformat(),
                "end": end_day.isoformat(),
            },
            "prev_window": {
                "start": prev_start.isoformat(),
                "end": prev_end.isoformat(),
            },
            "overall": overall_now,
            "overall_prev_window": overall_prev,
            "subscriber_summary": subscriber_summary,
            "subscribers_error": subscribers_error,
            "games_summary": games_summary,
            "games_error": games_error,
            "top_content_hits": top_content_hits,
            "sponsor_clicks_by_issue": sponsor_clicks_by_issue,
            "immersion_clicks": immersion_clicks,
            "flows_window": flows,
            "flows_error": flows_error,
            "skipped_journeys": skipped_journeys,
            "unmatched_sponsors": unmatched_sponsors,
            "report_text": report_text,
        },
    }
