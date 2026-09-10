"""
Builds an Excel workbook with a PAIR of sheets per category:

  "<Category>"           — Top 20 articles in that category, ranked by total
      unique clicks (summed across every campaign it was ever placed in).
      Per article:
        - max_unique_clicks / max_non_unique_clicks — its BEST single-campaign
          performance (the most clicks it ever got in one placement)
        - times_inserted_in_campaigns — how many distinct campaigns it ran in
        - total_unique_clicks / total_non_unique_clicks — summed across all
          those campaigns
        - avg_unique_clicks_per_insertion / avg_non_unique_clicks_per_insertion
          — total / times inserted, i.e. average performance per placement

  "<Category>_detailed"  — for those SAME Top 20 articles, one row per
      campaign they were placed in: which campaign (issue_name/issue_date),
      where it ran (story_position/position_category), and its clicks /
      unique clicks for that specific placement — same unique-before-clicks
      column order as the summary sheet.

Multi-category articles (e.g. "Fitness, Longevity") are split so the article
appears once under EACH category it's tagged with, in both the summary and
detail sheet for that category.

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
# lifetime totals, and average clicks per insertion. `norm_url` is carried
# through so Python can filter the detail query down to exactly these
# articles per category without re-matching on title text.
TOP_BY_CATEGORY_SQL = f"""
    WITH {AC_CTE},
    {WA_CTE},
    joined AS (
        SELECT
            ac.article_title AS title,
            ac.url,
            ac.norm_url,
            wa.categories,
            ac.issue_name,
            ac.unique_clicks,
            ac.non_unique_clicks
        FROM ac
        INNER JOIN wa ON ac.norm_url = wa.norm_url
    ),
    agg AS (
        SELECT
            title, url, norm_url, categories,
            COUNT(DISTINCT issue_name) AS times_inserted_in_campaigns,
            SUM(unique_clicks)         AS total_unique_clicks,
            SUM(non_unique_clicks)     AS total_non_unique_clicks,
            MAX(unique_clicks)         AS max_unique_clicks,
            MAX(non_unique_clicks)     AS max_non_unique_clicks
        FROM joined
        GROUP BY title, url, norm_url, categories
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
            title, url, norm_url,
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
        category, title, url, norm_url,
        max_unique_clicks, max_non_unique_clicks,
        times_inserted_in_campaigns,
        total_unique_clicks, total_non_unique_clicks,
        avg_unique_clicks_per_insertion, avg_non_unique_clicks_per_insertion
    FROM ranked
    WHERE rn <= {TOP_N}
    ORDER BY category, rn;
"""

# Every placement, with categories split the same way, so Python can filter
# down to just the Top-N articles per category for each "<Category>_detailed"
# sheet. Column order matches the summary sheet's unique-before-non-unique
# convention.
ALL_PLACEMENTS_BY_CATEGORY_SQL = f"""
    WITH {AC_CTE},
    {WA_CTE},
    joined AS (
        SELECT
            ac.article_title AS title,
            ac.url,
            ac.norm_url,
            wa.categories,
            ac.issue_name,
            ac.issue_date,
            ac.story_position,
            ac.position_category,
            ac.unique_clicks,
            ac.non_unique_clicks
        FROM ac
        INNER JOIN wa ON ac.norm_url = wa.norm_url
    )
    SELECT
        TRIM(cat) AS category,
        title, url, norm_url,
        issue_name, issue_date, story_position, position_category,
        unique_clicks, non_unique_clicks
    FROM joined
    CROSS JOIN LATERAL unnest(string_to_array(categories, ',')) AS cat
    ORDER BY category, title, issue_date;
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

# Column-name substrings that get a thousands-separator number format.
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
_DETAIL_SUFFIX = "_detailed"


def _category_sheet_names(category: str, taken: set) -> tuple:
    """Returns (summary_name, detail_name) for a category — both fit within
    Excel's 31-char limit (reserving room for `_detailed` on the base name
    so the pair always shares a matching prefix), and both are unique
    against `taken` (checked/added for both names together)."""
    cleaned = "".join(c if c not in _INVALID_SHEET_CHARS else "-" for c in str(category)).strip()
    cleaned = cleaned or "Uncategorized"
    max_base = 31 - len(_DETAIL_SUFFIX)
    base = cleaned[:max_base]

    candidate_base = base
    suffix_num = 2
    while (candidate_base.lower() in taken
           or f"{candidate_base}{_DETAIL_SUFFIX}".lower() in taken):
        tail = f" ({suffix_num})"
        candidate_base = base[: max_base - len(tail)] + tail
        suffix_num += 1

    summary_name = candidate_base
    detail_name = f"{candidate_base}{_DETAIL_SUFFIX}"
    taken.add(summary_name.lower())
    taken.add(detail_name.lower())
    return summary_name, detail_name


def main():
    conn = get_connection()
    try:
        print(f"Running Top-{TOP_N}-by-category query...")
        df_top = pd.read_sql(TOP_BY_CATEGORY_SQL, conn)

        print("Running all-placements-by-category query...")
        df_all_placements = pd.read_sql(ALL_PLACEMENTS_BY_CATEGORY_SQL, conn)
    finally:
        conn.close()

    with pd.ExcelWriter(OUT_FILE, engine="openpyxl") as writer:
        taken_names = set()
        category_count = 0

        for category, group in df_top.groupby("category", sort=True):
            summary_name, detail_name = _category_sheet_names(category, taken_names)

            # Summary sheet — drop the join-only columns before writing.
            summary_df = (
                group.drop(columns=["category", "norm_url"]).reset_index(drop=True)
            )
            summary_df.to_excel(writer, sheet_name=summary_name, index=False)
            _format_sheet(writer.sheets[summary_name], summary_df)

            # Detail sheet — every campaign placement, but ONLY for the
            # articles that made this category's Top N (matched on norm_url,
            # the same key the SQL used to dedupe article identity).
            top_urls_this_cat = set(group["norm_url"])
            detail_df = (
                df_all_placements[
                    (df_all_placements["category"] == category)
                    & (df_all_placements["norm_url"].isin(top_urls_this_cat))
                ]
                .drop(columns=["category", "norm_url"])
                .reset_index(drop=True)
            )
            detail_df.to_excel(writer, sheet_name=detail_name, index=False)
            _format_sheet(writer.sheets[detail_name], detail_df)

            category_count += 1

    print(f"\n✓ Wrote {OUT_FILE}")
    print(f"  {category_count} categories -> {category_count * 2} sheets "
          f"(summary + _detailed pairs)")
    print(f"  Top-by-category rows : {len(df_top):,}")
    print(f"  Placement rows total : {len(df_all_placements):,}")


if __name__ == "__main__":
    main()
