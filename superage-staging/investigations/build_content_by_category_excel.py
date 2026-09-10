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
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

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


# ─────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────

HEADER_FILL = PatternFill("solid", fgColor="1F2937")   # dark slate
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
BORDER_THIN = Border(*([Side(style="thin", color="D9D9D9")] * 4))
LINK_FONT   = Font(color="1155CC", underline="single")

# Column-name substrings that get a thousands-separator integer format.
COUNT_COL_HINTS = ("clicks", "times_inserted", "story_position")


def _format_sheet(ws, df: pd.DataFrame):
    """Applies header styling, column widths, number/date formats, a
    frozen header row, banded-row table styling, and turns any `url`
    column into a clickable hyperlink."""
    n_rows, n_cols = df.shape
    if n_rows == 0 or n_cols == 0:
        return

    # Header row styling
    for col_idx, col_name in enumerate(df.columns, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER_THIN

    # Per-column width, number format, borders, and (for `url`) hyperlinks.
    for col_idx, col_name in enumerate(df.columns, start=1):
        letter = get_column_letter(col_idx)
        series = df[col_name]
        max_content_len = series.astype(str).map(len).max() if n_rows else 0
        width = min(max(len(str(col_name)), int(max_content_len)) + 2, 60)
        ws.column_dimensions[letter].width = width

        is_count_col = any(hint in col_name.lower() for hint in COUNT_COL_HINTS)
        is_date_col  = "date" in col_name.lower()
        is_url_col   = col_name.lower() == "url"

        for row_idx in range(2, n_rows + 2):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = BORDER_THIN
            if is_count_col:
                cell.number_format = "#,##0"
                cell.alignment = Alignment(horizontal="right")
            elif is_date_col:
                cell.number_format = "yyyy-mm-dd"
            elif is_url_col and cell.value:
                cell.hyperlink = cell.value
                cell.font = LINK_FONT

    # Freeze header row (and Category/Title column where present) so it stays
    # visible while scrolling long sheets.
    freeze_col = 2 if df.columns[0].lower() in ("category",) else 1
    ws.freeze_panes = ws.cell(row=2, column=freeze_col + 1) if freeze_col == 2 else "A2"

    # Banded-row table styling over the full data range.
    last_col_letter = get_column_letter(n_cols)
    table_ref = f"A1:{last_col_letter}{n_rows + 1}"
    safe_name = "tbl_" + "".join(c if c.isalnum() else "_" for c in ws.title)
    table = Table(displayName=safe_name, ref=table_ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium9", showFirstColumn=False,
        showLastColumn=False, showRowStripes=True, showColumnStripes=False,
    )
    ws.add_table(table)

    ws.sheet_view.showGridLines = False


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

        for sheet_name, df in (
            ("Top Articles by Category", df1),
            ("Campaign Insertions", df2),
            ("Article Placements", df3),
        ):
            _format_sheet(writer.sheets[sheet_name], df)

    print(f"\n✓ Wrote {OUT_FILE}")
    print(f"  Sheet 1 — Top Articles by Category : {len(df1):,} rows")
    print(f"  Sheet 2 — Campaign Insertions       : {len(df2):,} rows")
    print(f"  Sheet 3 — Article Placements        : {len(df3):,} rows")


if __name__ == "__main__":
    main()
