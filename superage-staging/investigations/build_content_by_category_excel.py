"""
Builds a 3-sheet Excel workbook answering:

  Sheet 1 "Top Articles by Category" — Top 20 articles per category, ranked
      by unique clicks. Multi-category articles (e.g. "Fitness, Longevity")
      are split so the article appears once under EACH category it's
      tagged with.

  Sheet 2 "Campaign Insertions" — how many distinct campaigns (issue_name)
      each article was inserted into, all-time.

  Sheet 3 "Article Placements"  — one row per (article x campaign)
      occurrence, showing where in that campaign it ran
      (story_position / position_category) and its per-send clicks.

Scope: editorial content only — excludes type IN ('games','waitlist'),
same filter the dashboard's Content Reference tab already uses. All-time
(no date window). Matched to wordpress_articles via the same normalized-URL
join the dashboard's content-drill query uses (strips query string + www.).

Usage:
    export DB_HOST=your-rds-host.rds.amazonaws.com
    export DB_PORT=5432          # default 5432
    export DB_NAME=your_db
    export DB_USER=your_user
    export DB_PASSWORD=your_password
    export SA_SCHEMA=superage    # default: superage

    cd superage-staging/
    python investigations/build_content_by_category_excel.py

Output:
    superage-staging/investigations/content_by_category.xlsx
"""

import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

HERE = Path(__file__).parent
OUT_FILE = HERE / "content_by_category.xlsx"

S = os.environ.get("SA_SCHEMA", "superage")
TOP_N = int(os.environ.get("TOP_N_PER_CATEGORY", "20"))

# Editorial-only filter — same as the dashboard's content-drill query.
AC_TYPE_EXCL = "LOWER(COALESCE(type,'')) NOT IN ('games','waitlist')"
WP_FILTER = "(published_date IS NULL OR published_date::date < CURRENT_DATE)"

# Shared normalized-URL expression: strip query string + optional www., lowercase,
# trim trailing slash. Used to join articles_clicks.url <-> wordpress_articles.article_url.
NORM_URL = (
    "REGEXP_REPLACE(LOWER(TRIM(BOTH '/' FROM SPLIT_PART({col}, '?', 1))), "
    "'^https?://(www[.])?', '')"
)

AC_CTE = f"""
    ac AS (
        SELECT
            article_title, url, issue_name, issue_date,
            story_position, position_category,
            unique_clicks, non_unique_clicks,
            {NORM_URL.format(col='url')} AS norm_url
        FROM {S}.articles_clicks
        WHERE {AC_TYPE_EXCL}
    )
"""

WA_CTE = f"""
    wa AS (
        SELECT DISTINCT ON ({NORM_URL.format(col='article_url')})
            article_url,
            COALESCE(NULLIF(TRIM(categories), ''), 'Uncategorized') AS categories,
            {NORM_URL.format(col='article_url')} AS norm_url
        FROM {S}.wordpress_articles
        WHERE {WP_FILTER}
        ORDER BY {NORM_URL.format(col='article_url')}, modified_date DESC NULLS LAST
    )
"""

SHEET1_SQL = f"""
    WITH {AC_CTE},
    {WA_CTE},
    joined AS (
        SELECT
            ac.article_title AS title,
            ac.url,
            wa.categories,
            SUM(ac.unique_clicks)     AS unique_clicks,
            SUM(ac.non_unique_clicks) AS non_unique_clicks
        FROM ac
        INNER JOIN wa ON ac.norm_url = wa.norm_url
        GROUP BY ac.article_title, ac.url, wa.categories
    ),
    split_cat AS (
        SELECT title, url, unique_clicks, non_unique_clicks, TRIM(cat) AS category
        FROM joined
        CROSS JOIN LATERAL unnest(string_to_array(categories, ',')) AS cat
    ),
    ranked AS (
        SELECT
            category, title, url, unique_clicks, non_unique_clicks,
            ROW_NUMBER() OVER (PARTITION BY category ORDER BY unique_clicks DESC NULLS LAST) AS rn
        FROM split_cat
    )
    SELECT category, title, url, unique_clicks, non_unique_clicks
    FROM ranked
    WHERE rn <= {TOP_N}
    ORDER BY category, rn;
"""

SHEET2_SQL = f"""
    WITH {AC_CTE},
    {WA_CTE}
    SELECT
        ac.article_title AS title,
        ac.url,
        wa.categories,
        COUNT(DISTINCT ac.issue_name) AS times_inserted_in_campaigns,
        SUM(ac.unique_clicks)         AS total_unique_clicks,
        SUM(ac.non_unique_clicks)     AS total_non_unique_clicks
    FROM ac
    INNER JOIN wa ON ac.norm_url = wa.norm_url
    GROUP BY ac.article_title, ac.url, wa.categories
    ORDER BY times_inserted_in_campaigns DESC, total_unique_clicks DESC;
"""

SHEET3_SQL = f"""
    WITH {AC_CTE},
    {WA_CTE}
    SELECT
        ac.article_title AS title,
        ac.url,
        wa.categories,
        ac.issue_name,
        ac.issue_date,
        ac.story_position,
        ac.position_category,
        ac.unique_clicks,
        ac.non_unique_clicks
    FROM ac
    INNER JOIN wa ON ac.norm_url = wa.norm_url
    ORDER BY ac.article_title, ac.issue_date;
"""


def get_connection():
    required = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"\nMissing required env vars: {', '.join(missing)}\n"
            "Set DB_HOST, DB_NAME, DB_USER, DB_PASSWORD and re-run.\n"
        )
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=30,
    )


def main():
    conn = get_connection()
    try:
        print(f"Running Sheet 1 query (top {TOP_N} per category)...")
        df1 = pd.read_sql(SHEET1_SQL, conn)

        print("Running Sheet 2 query (campaign insertion counts)...")
        df2 = pd.read_sql(SHEET2_SQL, conn)

        print("Running Sheet 3 query (per-campaign placements)...")
        df3 = pd.read_sql(SHEET3_SQL, conn)
    finally:
        conn.close()

    with pd.ExcelWriter(OUT_FILE, engine="openpyxl") as writer:
        df1.to_excel(writer, sheet_name="Top Articles by Category", index=False)
        df2.to_excel(writer, sheet_name="Campaign Insertions", index=False)
        df3.to_excel(writer, sheet_name="Article Placements", index=False)

    print(f"\n✓ Wrote {OUT_FILE}")
    print(f"  Sheet 1 — Top Articles by Category : {len(df1):,} rows")
    print(f"  Sheet 2 — Campaign Insertions       : {len(df2):,} rows")
    print(f"  Sheet 3 — Article Placements        : {len(df3):,} rows")


if __name__ == "__main__":
    main()
