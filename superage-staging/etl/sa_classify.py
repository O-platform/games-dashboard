"""
sa_classify.py
--------------
Shared URL classification and sponsor matching logic.
Imported by both local full-load scripts and both Lambda handlers.

URL normalisation rules:
  - Strip query params / UTM tokens before any matching
  - Resolve o.superage.com/r?dest=... redirects
  - Editorial covers:
      superage.com/<slug>
      superage.com/article/<slug>     (case-insensitive)
      superage.com/articles/<slug>    (case-insensitive)
  - games   = games.superage.com/**
              OR o.superage.com/r?dest=games.superage.com
  - waitlist = superage.com/games/**
  - Sponsor short-token guard: tokens < 6 chars matched on HOST only
    to prevent utm_source=our triggering "Our Place"

Airtable URL matching normalises trailing slash so
  https://superage.com/is-it-burnout-or-menopause-my-brain-fog-was-the-first-sign/
matches the same key as the version without trailing slash.

═══════════════════════════════════════════════════════════════════════
FIX (this revision) — sponsor clean_url no longer keeps query params
═══════════════════════════════════════════════════════════════════════
Previously, classify_url() returned the FULL raw URL (with its query
string intact) for sponsor-type clicks:
    return "sponsor", ensure_scheme(raw_url.strip()), sp
Sponsor links almost always carry a per-subscriber tracking token in that
query string (e.g. ?oid=abc123, ?hashed_email=...) — a different value on
every single click. Since this value becomes part of articles_clicks'
identity key (issue_name, url, article_title) in
lambda_clicks_incremental.py, nearly every sponsor click created its OWN
brand-new row instead of accumulating onto one row per (article, issue).

Sales matching (enrich_sponsor_names() in lambda_clicks_incremental.py)
does NOT actually need the query string preserved here — it strips query
params from both the stored articles_clicks.url AND the Airtable sales
tracking links before comparing (SPLIT_PART(url,'?',1) on both sides), so
matching still works identically with the query stripped at this earlier
stage. Sponsor now returns the same query-stripped clean_url every other
type already used, closing off the fragmentation at its source rather
than only correcting the resulting numbers downstream.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

SUPPORTED_TYPES = {"editorial", "sponsor", "immersion", "waitlist", "games", "affiliate", "cpl"}

SUPERAGE_HOST   = "superage.com"
IMMERSIONS_PATH = "/immersions"
GAMES_PATH      = "/games"
SKIP_PATHS      = {"terms", "disclaimers", "privacy", "partnerships"}
ARTICLE_PATH_PREFIXES = {"article", "articles"}
CPL_PATTERNS_LIST     = ["ageist", "allhealthy", "all-healthy", "healthbrief", "health-brief"]

SPONSOR_RX   = re.compile(r"\(Sponsor:\s*([^)]+)\)", re.I)
MULTI_SEP_RX = re.compile(r"\s*(?:\+|/|\band\b)\s*", re.I)

SPONSOR_TOKEN_STOPWORDS = {
    "a", "an", "and", "at", "by", "co", "com", "for", "from", "get",
    "health", "inc", "life", "of", "our", "place", "the", "to", "with",
}

# tokens shorter than this are matched host-only to avoid UTM false positives
SHORT_TOKEN_HOST_ONLY_THRESHOLD = 6

DEFAULT_SPONSOR_PATTERNS: List[Tuple[str, List[str]]] = [
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
]

# ═══════════════════════════════════════════════════════════════
# URL helpers
# ═══════════════════════════════════════════════════════════════

def ensure_scheme(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, flags=re.I):
        return "https://" + u
    return u


def normalize_host_from_parsed(parsed) -> str:
    return (parsed.hostname or "").lower().replace("www.", "")


def normalize_host(url: str) -> str:
    try:
        return normalize_host_from_parsed(urlparse(ensure_scheme(url)))
    except Exception:
        return ""


def effective_url(raw_url: str) -> str:
    """Resolve o.superage.com/r?dest=... redirects."""
    if not raw_url:
        return ""
    raw_url = raw_url.strip()
    try:
        parsed = urlparse(ensure_scheme(raw_url))
        host   = normalize_host_from_parsed(parsed)
        qs     = parse_qs(parsed.query)
        if host == "o.superage.com" and parsed.path.rstrip("/") == "/r" and qs.get("dest"):
            return ensure_scheme(unquote(qs["dest"][0]).strip())
    except Exception:
        pass
    return raw_url


def strip_query(url: str) -> str:
    """Remove query string, fragment, and trailing slash.

    NOTE: lowercases scheme + host but intentionally leaves the path's
    case as-is (the site's actual slugs are already lowercase in
    practice; forcing lowercase here would be a separate, larger change
    to verify against real data — left alone in this revision).
    """
    try:
        u      = urlparse(ensure_scheme(url))
        scheme = (u.scheme or "https").lower()
        netloc = (u.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = re.sub(r"/+$", "", u.path or "")
        return urlunparse((scheme, netloc, path, "", "", ""))
    except Exception:
        return url


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def first_path_segment(url: str) -> str:
    try:
        segs = [s for s in urlparse(ensure_scheme(url)).path.split("/") if s]
        return segs[0].lower() if segs else ""
    except Exception:
        return ""


def is_root_superage(clean_url: str) -> bool:
    try:
        p = urlparse(ensure_scheme(clean_url))
        return (normalize_host_from_parsed(p) == SUPERAGE_HOST
                and p.path.strip("/") == "")
    except Exception:
        return False


def slug_to_title(url: str) -> str:
    """
    Convert a URL path to a human-readable title.
    Handles trailing slash, /article/, /articles/ prefixes.
    Example:
      superage.com/is-it-burnout-or-menopause-my-brain-fog-was-the-first-sign/
      → "Is It Burnout Or Menopause My Brain Fog Was The First Sign"
    """
    try:
        path = urlparse(ensure_scheme(url)).path
    except Exception:
        return url
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "Home"
    if segs[0].lower() in ARTICLE_PATH_PREFIXES and len(segs) > 1:
        segs = segs[1:]
    slug = re.sub(r"\.html?$", "", segs[-1], flags=re.I).replace("_", "-")
    return " ".join(w.capitalize() for w in slug.split("-") if w) or url


def immersion_title(url: str) -> str:
    try:
        path   = urlparse(ensure_scheme(url)).path
        marker = IMMERSIONS_PATH.rstrip("/").lower()
        low    = path.lower()
        if marker in low:
            remainder = path[low.index(marker) + len(marker):].strip("/")
            first     = remainder.split("/")[0] if remainder else ""
            if first:
                return " ".join(w.capitalize() for w in first.split("-") if w)
    except Exception:
        pass
    return slug_to_title(url)


def affiliate_title(clean_url: str) -> str:
    try:
        parsed = urlparse(ensure_scheme(clean_url))
        host   = normalize_host_from_parsed(parsed)
        path   = parsed.path.strip("/")
        if path:
            return f"Affiliate - {host}/{path.split('/')[0]}"
        return f"Affiliate - {host}"
    except Exception:
        return "Affiliate"


# ═══════════════════════════════════════════════════════════════
# Sponsor matching
# ═══════════════════════════════════════════════════════════════

def sponsor_tokens(name: str) -> List[str]:
    if not name:
        return []
    lower  = re.sub(r"[&+/]", " ", name.lower())
    parts  = [p for p in re.sub(r"\s+", " ", lower).strip().split() if p]
    tokens: List[str] = []
    full_compact = compact(name)
    if len(full_compact) >= 4:
        tokens.append(full_compact)
    for p in parts:
        p_clean = compact(p)
        if len(p_clean) >= 4 and p_clean not in SPONSOR_TOKEN_STOPWORDS:
            tokens.append(p_clean)
    phrase = compact(" ".join(parts))
    if len(phrase) >= 4:
        tokens.append(phrase)
    return list(dict.fromkeys(tokens))


def url_contains_any(url: str, patterns: List[str]) -> bool:
    """
    Short tokens (< SHORT_TOKEN_HOST_ONLY_THRESHOLD chars) matched host-only
    to prevent utm_source=our triggering "Our Place".
    """
    if not url:
        return False
    clean        = strip_query(url)
    full         = ensure_scheme(clean).lower()
    host         = normalize_host(full)
    compact_full = compact(full)
    for pattern in patterns:
        p  = str(pattern).strip().lower()
        cp = compact(p)
        if not p:
            continue
        if len(cp) < SHORT_TOKEN_HOST_ONLY_THRESHOLD:
            if p in host or cp in compact(host):
                return True
        else:
            if p in host or p in full or (cp and cp in compact_full):
                return True
    return False


def extract_sponsor_names(issue_name: str) -> List[str]:
    if not issue_name:
        return []
    m = SPONSOR_RX.search(issue_name)
    if not m:
        return []
    return [p.strip() for p in MULTI_SEP_RX.split(m.group(1).strip()) if p.strip()]


_sponsor_map_cache: Optional[Dict[str, List[str]]] = None

def _sponsor_map() -> Dict[str, List[str]]:
    global _sponsor_map_cache
    if _sponsor_map_cache is None:
        out: Dict[str, List[str]] = defaultdict(list)
        for name, patterns in DEFAULT_SPONSOR_PATTERNS:
            out[name.strip().lower()].extend(str(p).lower() for p in patterns if str(p).strip())
        _sponsor_map_cache = dict(out)
    return _sponsor_map_cache


def match_issue_sponsor(url: str, issue_name: str) -> Optional[str]:
    for sponsor_name in extract_sponsor_names(issue_name or ""):
        patterns = list(_sponsor_map().get(sponsor_name.strip().lower(), []))
        patterns += sponsor_tokens(sponsor_name)
        if url_contains_any(url, patterns):
            return sponsor_name
    return None


def match_any_sponsor(url: str) -> Optional[str]:
    if not url:
        return None
    for name, patterns in DEFAULT_SPONSOR_PATTERNS:
        if url_contains_any(url, [str(p) for p in patterns]):
            return name
    return None


def match_cpl(url: str) -> Optional[str]:
    if not url:
        return None
    full         = ensure_scheme(url).lower()
    host         = normalize_host(full)
    compact_full = compact(full)
    for p in CPL_PATTERNS_LIST:
        cp = compact(p)
        if p in full or p in host or (cp and cp in compact_full):
            if "ageist"      in cp: return "CPL - Ageist"
            if "allhealthy"  in cp: return "CPL - AllHealthy"
            if "healthbrief" in cp: return "CPL - HealthBrief"
            return f"CPL - {p}"
    return None


# ═══════════════════════════════════════════════════════════════
# Classification entry point
# ═══════════════════════════════════════════════════════════════

def classify_url(raw_url: str, issue_name: str) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Returns (type, clean_url, label).
    type is None for excluded URLs (root, utility pages).

    clean_url has query/fragment/trailing-slash stripped for EVERY type,
    including sponsor. Sponsor matching itself (url_contains_any) already
    runs against the pre-strip `url`/`raw_url`, so stripping the query
    afterward for the returned clean_url does not change WHICH sponsor
    gets matched — it only stops the per-subscriber tracking token
    (?oid=..., ?hashed_email=...) from becoming part of articles_clicks'
    identity key downstream. See module docstring for why this is safe
    for Sales matching (enrich_sponsor_names strips query params on both
    sides anyway).
    """
    if not raw_url:
        return None, "", None

    url       = ensure_scheme(effective_url(raw_url))
    clean_url = strip_query(url)
    parsed    = urlparse(clean_url)
    host      = normalize_host_from_parsed(parsed)
    path      = parsed.path.lower().rstrip("/") or ""
    first_seg = first_path_segment(clean_url)

    if host == SUPERAGE_HOST and is_root_superage(clean_url):
        return None, clean_url, None
    if host == SUPERAGE_HOST and first_seg in SKIP_PATHS:
        return None, clean_url, None

    if host == "games.superage.com" or host.endswith(".games.superage.com"):
        return "games", clean_url, None
    if host == SUPERAGE_HOST and (path == GAMES_PATH or path.startswith(GAMES_PATH + "/")):
        return "waitlist", clean_url, None
    if host == SUPERAGE_HOST and (path == IMMERSIONS_PATH or path.startswith(IMMERSIONS_PATH + "/")):
        return "immersion", clean_url, None
    if host == SUPERAGE_HOST:
        return "editorial", clean_url, None

    cpl = match_cpl(url)
    if cpl:
        return "cpl", clean_url, cpl

    sp = match_issue_sponsor(url, issue_name)
    if sp:
        return "sponsor", clean_url, sp

    gsp = match_any_sponsor(url)
    if gsp:
        return "sponsor", clean_url, gsp

    return "affiliate", clean_url, None


def compute_position_category(rank: int, total: int,
                               high_pct: float = 0.30,
                               medium_pct: float = 0.70) -> str:
    """
    Splits articles into high / medium / low by position rank.

    high_pct=0.30, medium_pct=0.70 (defaults):
      5 articles: ceil(5*0.30)=2 high, ceil(5*0.70)=4 → 2 medium, 1 low
      3 articles: ceil(3*0.30)=1 high, ceil(3*0.70)=3 → 2 medium, 0 low
      2 articles: ceil(2*0.30)=1 high, ceil(2*0.70)=2 → 1 medium, 0 low
      10 articles: ceil(10*0.30)=3 high, ceil(10*0.70)=7 → 4 medium, 3 low
    """
    high_cut = max(1, math.ceil(total * high_pct))
    med_cut  = max(high_cut, math.ceil(total * medium_pct))
    if rank <= high_cut: return "high"
    if rank <= med_cut:  return "medium"
    return "low"


def article_title_for(click_type: str, clean_url: str, label: Optional[str]) -> str:
    if click_type == "sponsor":   return label or slug_to_title(clean_url)
    if click_type == "games":     return "Games"
    if click_type == "waitlist":  return "Waitlist"
    if click_type == "immersion": return immersion_title(clean_url)
    if click_type == "affiliate": return affiliate_title(clean_url)
    if click_type == "cpl":       return label or "CPL"
    return slug_to_title(clean_url)


# ═══════════════════════════════════════════════════════════════
# Airtable enrichment helpers (shared)
# ═══════════════════════════════════════════════════════════════

def normalize_url_key(raw_url: str) -> str:
    """
    Canonical key for URL matching against Airtable.
    Strips trailing slash, query, www, resolves redirects.
    For sponsor URLs (which retain UTMs) this still strips query params
    so the key is clean for dict lookup.
    """
    if not raw_url:
        return ""
    u = ensure_scheme(effective_url(raw_url))
    try:
        parsed = urlparse(u)
        host   = (parsed.hostname or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        path = re.sub(r"/+$", "", parsed.path or "")
        if host == "games.superage.com":
            return "games.superage.com"
        if host == "superage.com" and (path == "/games" or path.startswith("/games/")):
            return "superage.com/games"
        if path in ("", "/"):
            return host
        return f"{host}{path}".lower()
    except Exception:
        u = re.sub(r"^https?://", "", raw_url.strip(), flags=re.I)
        u = re.sub(r"^www\.", "", u, flags=re.I)
        return u.split("?")[0].split("#")[0].rstrip("/").lower()


def parse_int(value: Any) -> Optional[int]:
    if value is None: return None
    if isinstance(value, (int, float)): return int(value)
    m = re.search(r"-?\d+", str(value).strip())
    return int(m.group(0)) if m else None


def parse_date_value(value: Any) -> Optional[date]:
    if value is None: return None
    if isinstance(value, date) and not isinstance(value, datetime): return value
    if isinstance(value, datetime): return value.date()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(value).strip())
    if not m: return None
    try: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception: return None


def parse_issue_date_from_name(issue_name: str) -> Optional[date]:
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", issue_name or "")
    if not m: return None
    try: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception: return None
