"""
Builds a 2-sheet-type Excel workbook answering:

  One sheet PER CATEGORY — "Top 20 by Category" — Top 20 articles in that
      category, ranked by total unique clicks (summed across every campaign
      it was ever placed in). Multi-category articles (e.g. "Fitness,
      Longevity") are split so the article appears once under EACH category
      it's tagged with. Per article:
        - max_unique_clicks / max_non_unique_clicks — its BEST single-campaign
          performance (the most clicks it ever got in one placement)
        - times_inserted_in_campaigns — how many distinct campaigns it ran in
        - total_unique_clicks / total_non_unique_clicks — summed across all
          those campaigns
        - avg_unique_clicks_per_insertion / avg_non_unique_clicks_per_insertion
          — total / times inserted, i.e. average performance per placement

  "Placements" (single sheet) — one row per (article x campaign) occurrence,
      showing where in that campaign it ran (story_position /
      position_category) and its per-send clicks.

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

# Top N per category — peak single-campaign performance, times inserted,
# lifetime totals, and average clicks per insertion.
TOP_BY_CATEGORY_SQL = f"""
    WITH {AC_CTE},
    {WA_CTE},
    joined AS (
        SELECT
            ac.article_title AS title,
            ac.url,
            wa.categories,
            ac.issue_name,
            ac.unique_clicks,
            ac.non_unique_clicks
        FROM ac
        INNER JOIN wa ON ac.norm_url = wa.norm_url
    ),
    agg AS (
        SELECT
            title, url, categories,
            COUNT(DISTINCT issue_name) AS times_inserted_in_campaigns,
            SUM(unique_clicks)         AS total_unique_clicks,
            SUM(non_unique_clicks)     AS total_non_unique_clicks,
            MAX(unique_clicks)         AS max_unique_clicks,
            MAX(non_unique_clicks)     AS max_non_unique_clicks
        FROM joined
        GROUP BY title, url, categories
    ),
    with_avg AS (
        SELECT
            *,
            ROUND(total_unique_clicks::numeric
                  / NULLIF(times_inserted_in_campaigns, 0), 2) AS avg_unique_clicks_per_insertion,
            ROUND(total_non_unique_clicks::numeric
                  / NULLIF(times_inserted_in_campaigns, 0), 2) AS avg_non_unique_clicks_per_insertion
        FROM agg
    ),
    split_cat AS (
        SELECT
            title, url,
            max_unique_clicks, max_non_unique_clicks,
            times_inserted_in_campaigns,
            total_unique_clicks, total_non_unique_clicks,
            avg_unique_clicks_per_insertion, avg_non_unique_clicks_per_insertion,
            TRIM(cat) AS category
        FROM with_avg
        CROSS JOIN LATERAL unnest(string_to_array(categories, ',')) AS cat
    ),
    ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY category ORDER BY total_unique_clicks DESC NULLS LAST) AS rn
        FROM split_cat
    )
    SELECT
        category, title, url,
        max_unique_clicks, max_non_unique_clicks,
        times_inserted_in_campaigns,
        total_unique_clicks, total_non_unique_clicks,
        avg_unique_clicks_per_insertion, avg_non_unique_clicks_per_insertion
    FROM ranked
    WHERE rn <= {TOP_N}
    ORDER BY category, rn;
"""

PLACEMENTS_SQL = f"""
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
        max_content_len = (
            series.map(lambda v: len(str(v)) if pd.notna(v) else 0).max() if n_rows else 0
        )
        width = min(max(len(str(col_name)), int(max_content_len)) + 2, 60)
        ws.column_dimensions[letter].width = width

        is_count_col = any(hint in col_name.lower() for hint in COUNT_COL_HINTS)
        is_date_col  = "date" in col_name.lower()
        is_url_col   = col_name.lower() == "url"

        for row_idx in range(2, n_rows + 2):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = BORDER_THIN
            if is_count_col:
                cell.number_format = "#,##0.##"
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


# Excel sheet names: max 31 chars, and none of : \ / ? * [ ]
_INVALID_SHEET_CHARS = set(':\\/?*[]')


def _safe_sheet_name(name: str, taken: set) -> str:
    """Sanitizes a category name into a valid, unique Excel sheet name."""
    cleaned = "".join(c if c not in _INVALID_SHEET_CHARS else "-" for c in str(name)).strip()
    cleaned = cleaned or "Uncategorized"
    base = cleaned[:31]
    candidate = base
    suffix = 2
    while candidate.lower() in taken:
        tail = f" ({suffix})"
        candidate = base[: 31 - len(tail)] + tail
        suffix += 1
    taken.add(candidate.lower())
    return candidate


def main():
    conn = get_connection()
    try:
        print(f"Running Top-{TOP_N}-by-category query...")
        df_top = pd.read_sql(TOP_BY_CATEGORY_SQL, conn)

        print("Running Placements query...")
        df_placements = pd.read_sql(PLACEMENTS_SQL, conn)
    finally:
        conn.close()

    with pd.ExcelWriter(OUT_FILE, engine="openpyxl") as writer:
        # One sheet PER category. `category` column is dropped from the sheet
        # body since it's now implied by the sheet name itself. Groups keep
        # the SQL's existing rn ordering (rank by total_unique_clicks).
        taken_names = set()
        category_sheet_count = 0
        for category, group in df_top.groupby("category", sort=True):
            sheet_df = group.drop(columns=["category"]).reset_index(drop=True)
            sheet_name = _safe_sheet_name(category, taken_names)
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)
            _format_sheet(writer.sheets[sheet_name], sheet_df)
            category_sheet_count += 1

        df_placements.to_excel(writer, sheet_name="Placements", index=False)
        _format_sheet(writer.sheets["Placements"], df_placements)

    print(f"\n✓ Wrote {OUT_FILE}")
    print(f"  Category sheets ({category_sheet_count} total) : {len(df_top):,} rows")
    print(f"  Placements sheet                    : {len(df_placements):,} rows")


if __name__ == "__main__":
    main()
