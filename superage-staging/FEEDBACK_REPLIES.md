# Feedback — Replies

Each item below is one of your dashboard comments, paired with what was done
and where to find it. Items marked **✓ shipped** are live on `main` and visible
once the comparison / metrics lambdas re-run on schedule. Items marked
**ℹ already in place** were already implemented before this round of feedback.

---

## Subscriber Retention

### "MONTHLY CHURN VOLUME — L1 VS L2, why are the curves not aligned? it seems like they are shifted"

**ℹ Obsolete.** L1 / L2 tiers were removed earlier; the chart is now a single
churn-count series across all subscribers. Confirmed in your follow-up
("i removed the L1 vs L2 so we don't need it at all"). Doc cleanup:
`METRICS_updated.md` § 7 had stale "Tracks L1 and L2 subscriber tiers" copy
— **rewritten** in this round to reflect the single-series implementation.

### "I dont understand the life span part, 117 and 120?"

**ℹ Obsolete.** With L1 / L2 gone, the Lifespan KPI card now shows one pair
(Avg + Median). For the current snapshot: **avg 118 days, median 81 days**
across all churned subscribers. The two numbers are different because the
median is dragged down by a long tail of short-lived sign-ups; the avg is
pulled up by long-time subscribers. Both still come from the same Q35 query.

### "Can we add a section to track each main source separately? Track AH CPL, Ageist CPL, Share, Meta, Google, Direct. LTV (life span), unsubscribe rate after 30 days, and after 15 days, clicks…"

**✓ shipped.** New section **Retention by Acquisition Source** under the
Subscriber Retention tab.

- **Where:** below the existing Survival / Lifespan / Churn charts.
- **Source labels:** mapped by SQL `CASE`:
  - `AH CPL` ← `ahcpl1`, `allhealthy`
  - `Ageist CPL` ← `theageist`, `ageist`
  - `Share` ← `share`, `referral`
  - `Meta` ← `meta`, `facebook`, `fb`, `if`
  - `Google` ← `google`
  - `Direct` ← `organic`, `direct`, empty
- **Columns:** Subscribers · Active Now · Churned · **Unsub ≤ 15 d** (count + %)
  · **Unsub ≤ 30 d** (count + %) · **Avg LTV** · **Median LTV** · Clickers ·
  Clicker Rate · Unique Clicks · Clicks / Clicker.
- **Files touched:**
  - SQL: `superage_metrics_lambda_updated.py` — new section "8b. Retention by Acquisition Source" (~lines 965-1030).
  - Serialiser: same file — `M["retention_by_source"]` payload.
  - HTML: `index.html` — table rendered inside `renderRetention()`.
  - Docs: `METRICS_updated.md` — new query **Q35b**.
- **Caveat:** populates on the next metrics-lambda run; until then the table
  renders empty (no client-side fallback because the data simply isn't in the
  JSON yet).

---

## Revenue & Sponsors

### "In table: TOP SPONSORS BY REVENUE, Can you add average revenue per Deal?"

**ℹ already in place.** Column **Avg Rev / Deal** exists in the Top Sponsors
table (computed client-side as `revenue / deals`). No change.

---

## Active (Send-to)

### "make sure that active across the dashboard is subscribers where engagement not in dormant ghost zombies"

**ℹ already in place** at the data layer — see `Q1b` in `METRICS_updated.md`:
`COUNT(*) FILTER (WHERE engagement_segment NOT IN ('Ghosts', 'Zombies', 'Dormant'))`.
This drives `M.send_to_active` (currently **712,660** / 64.1 % of total).

**Where it's used (kept):**
- Overview KPI "Active (Send-to)" — value = `send_to_active`.
- Subscriber Growth chart — "Total Subscribers" line uses `total_active` from
  the `growth_history` snapshot (which is the same send-to definition).

**Where state='Active' is *intentionally* still used (and labelled clearly):**
- Subscriber Status Mix donut — this chart is about the four CreateSend
  states (Active / Unsubscribed / Bounced / Deleted), so "Active" here means
  CreateSend-state Active. The two concepts can disagree by tens of thousands
  (state-Active includes Dormants).

> If you want me to *also* split Active into "Send-to" and "Dormant/Ghost/Zombie"
> slices inside the Status Mix donut, say the word and I'll add a second
> view. For now the donut keeps its CreateSend semantics so the % numbers
> match the rest of CreateSend.

---

## Website Content / Click Analysis tab

### "I feel like a better name for this tab is: Click Analysis, right?"

**ℹ already in place.** Tab is **Click Analysis** (`index.html` line 187).

### "Need a filter to show 7d, 15days, 30 days, 90 days?"

**ℹ already in place.** The **Top Articles by Unique Clicks** card has
`All / 7d / 15d / 30d / 90d` buttons (top-right of the table). Data is
pre-rolled in the metrics lambda as `top_articles_windowed`.

### "In the TAG PERFORMANCE TABLE, order by AVG Clicks, and name it 'clicks per placement'"

**ℹ already in place.** Header is **Clicks per Placement**, rows are sorted
`avg_unique_clicks` DESC.

### "In the 'TOP ARTICLES BY READER ENGAGEMENT' what is position 0?"

**ℹ already in place.** When `story_position` is null or 0, the cell renders
the italic muted text *"No position"* instead of `0`. The Content Reference
tab also has a dedicated "No position" option in the Position filter.

---

## Whole-dashboard comments

### "Update frequency missing"

**✓ shipped.** Header now shows `Data as of <date> · Refreshed daily`.
The `as-of-label` element also has a tooltip clarifying that the
comparison snapshot refreshes every 6 hours.

### "Quiz Takers, (add to the number the percentage out of total subs)"

**ℹ already in place.** KPI card sub-text reads `<N>% of total subscribers`,
sourced from `M.quiz_takers_pct` (currently 79.3 %).

### "Active Subscribers are Subscribers we send to … this needs to show in the top metrics, and also in the subscriber status mix"

**ℹ partially in place** — top metrics already use Send-to (see Active
note above). Status Mix donut intentionally keeps CreateSend-state semantics
so the % shown matches CreateSend. See the question raised in the Active
section if you want a different split inside the donut.

### "In the subscriber Status mix, I need to show percentages too"

**ℹ already in place.** A coloured legend appears under the donut with
`<Label> <pct>% (<count>)` for every state.

### "Review the word (Active) in the entire dashboard … e.g. SUBSCRIBER GROWTH OVER TIME … make it Total Subscribers instead of total Active"

**✓ shipped.** The line series previously labelled
`Total Subscribers (Active)` is now plain **`Total Subscribers`** in the
Subscriber Growth Over Time chart.

### "Edit TOTAL RECIPIENTS, to be total sent emails"

**✓ shipped.** Campaigns KPI card relabelled **Total Sent Emails**. The
matching column in the All-Campaigns table was already named "Total Sent Emails".

### "In Campaigns, can we add a filter (check box) to filter out Sunday Spotlight?"

**ℹ already in place.** Toggle at the top of the Campaigns tab (default ON =
include). Scopes every KPI, both charts, and the paginated table.

### "Can we have Campaigns Clickable? in the different places we mention the campaign, can we have hyperlink to click and open the campaigns?"

**ℹ already in place.** Campaign names are rendered as `<a href={URL} …>` in:
- Overview → Top Campaign Open Rates table.
- Campaigns → All Campaigns paginated table.

URLs come from the CreateSend `URL` column in `superage."Campaigns"` (see
`METRICS_updated.md` Q9 / Q10). If a campaign row has an empty URL the name
falls back to plain text.

---

## Quick file-level summary of this round

| File | Change |
|---|---|
| `superage-staging/index.html` | KPI label rename; growth chart legend rename; update-frequency note; new Retention-by-Source table inside `renderRetention()`. |
| `superage-staging/superage_metrics_lambda_updated.py` | New section 8b query + `M["retention_by_source"]` serialiser. |
| `superage-staging/METRICS_updated.md` | § 7 rewritten (no L1/L2); new sub-section "Retention by Acquisition Source"; new SQL block **Q35b**. |
| `superage-staging/FEEDBACK_REPLIES.md` | This file — your comment ledger. |

The new per-source table will render empty until the metrics lambda runs again
and writes `retention_by_source` into `superage-metrics.json` — the HTML is
already wired to it.

---

## Round 2 — May 14-15, 2026

### "The top numbers in order: Active (SEND-TO), AVG OPEN RATE, AVG CLICK RATE, CAMPAIGNS SENT. These are the top blocks to show, keep the others but have them in the second row (as they are less important)"

**✓ shipped.** Overview KPI cards split into two rows:
- **Primary row (4 cards):** Active (Send-to) · Avg Open Rate · Avg Click Rate · Campaigns Sent
- **Secondary row (4 cards):** Total Subscribers · Unsubscribed · Bounced · Quiz Takers

Both rows pinned to a 4-column grid so the ordering can't reflow on wide screens.

### "In overview page, this needs to be 'Recent Campaigns Metrics' and should have the last 10 campaigns, not the top 10"

**✓ shipped.** Renamed the bottom Overview table to **Recent Campaigns Metrics**.
Rows are now the **last 10 sent campaigns by date** (most recent first), sorted
client-side on `sent_date` DESC from `M.campaign_table`. No SQL change needed —
the data was already loaded for the Campaigns tab.

> **Heads-up:** When you first looked, `SA_PF_20260514` wasn't visible. That's
> a data-freshness issue, not a dashboard bug: the metrics lambda last ran on
> May 14 (`data_as_of: "May 14, 2026"`) and its SQL filter is
> `"Sent Date "::date < CURRENT_DATE` — so the May 14 campaign was excluded
> from that run's snapshot. It will appear on the next lambda invocation.

### "Love this check box, super helpful. Make this turned off by default" — Sunday Spotlight toggle

**✓ shipped.** Campaigns tab opens with **Sunday Spotlight excluded** by default
(toggle unchecked, label "Excluding branded digest sends"). KPIs, charts and
the paginated table all start scoped without Sunday Spotlight.

### "Let's keep the date in 1 row, by making column size a little bigger" — Sent Date wraps

**✓ shipped.** Added a `.nowrap` utility class to `dash-table` cells and applied
it to the Sent Date `<td>` in both the Campaigns paginated table and the
Overview Recent Campaigns Metrics table. Dates now stay on a single line even
when other columns crowd the row.

### Revenue chart future months — color + separator

**✓ shipped (from earlier in the round).** Past/current months render in solid
gold (`#7d4e00`); future months render in **red** (`rgba(207,34,46,0.55)`).
A vertical dashed separator labelled `Future →` sits between the last
past/current bar and the first future bar, drawn by an inline Chart.js plugin
(`futureSeparator`).

### "Let's have the standard dark and light mode we had before, or we have in the other dashboards … note this dashboard is daily update"

**✓ shipped.** Header restyled to match the other dashboards:

- A **`Daily updates`** pill (blue accent on the left) sits next to the
  `data_as_of` date in bold; below it a muted line reads
  `SuperAge — Brand Pulse · Last updated`.
- The old `Dark mode` button is now an **iOS-style switch** with a `DARK`
  label, mirroring the screenshot. State persists in `localStorage`
  (`sa-dark`) and the checkbox is synced to the saved state on load.

Because this dashboard is daily (not hourly), the pill says **"Daily updates"**
and the date renders date-only — no time/timezone needed.

### "Let's add the same (Sunday Spotlight) filter to the Clicks Analysis"

**✓ shipped.** Click Analysis tab now has an **Include Sunday Spotlight**
toggle at the top (default OFF — matches the Campaigns tab) that scopes
**both** sections:

- **Section 1 — Campaign-Level Aggregated** (Weekly / Monthly Campaign Clicks):
  rebuilt **client-side** from `M.campaign_table` so the filter takes effect
  immediately (no lambda re-run needed). Sunday Spotlight rows are skipped
  by `name` match before bucketing.
- **Section 2 — Raw Click Events** (Same Weekday / Weekly / Monthly): the
  comparison lambda now emits a `clicks_no_ss` count alongside `clicks` for
  every series, using
  `COUNT(*) FILTER (WHERE issue_name NOT ILIKE '%sunday spotlight%')`. The
  dashboard picks the right field based on the toggle, falling back to
  `clicks` if `clicks_no_ss` isn't present yet.

> Note: top-of-tab KPIs (Total Clicks, Unique Clickers, Articles Clicked,
> Avg Clicks/Article) and the **Top Articles / Category / Author / Tag**
> tables still aggregate from `articles_clicks` without the SS filter. If
> you also want those scoped by the toggle, that's a separate `articles_clicks`-
> based lambda change — happy to do it; just say the word.

### "Can we normalise the numbers? such as divide the clicks by number of campaigns, or is there another option because we also have different number of recipients?"

**✓ shipped.** Section 1 has a **Metric** dropdown next to the toggle with
three modes:

- **Total clicks** — the original raw sum (default).
- **Clicks / campaign (normalised)** — `sum(clicks) / count(campaigns)`,
  removes the bias from periods with more campaigns.
- **Click-to-Open %** — `sum(clicks) / sum(unique_opens) × 100`,
  normalises for the size of the engaged audience (closest equivalent to
  the click-rate idea you raised, but using `unique_opens` instead of
  `recipients`; the `campaign_table` doesn't carry `recipients` in the
  pre-aggregated trend series — we'd need a lambda field to do
  `clicks / recipients`. Let me know if you'd prefer that instead).

The y-axis tick formatter and tooltip update per mode (e.g. CTOR shows
`12.3 %`, per-campaign shows integers).

### "The 40.3% is misleading and not true … it's just an ongoing month / when compare try to compare between last 2 completed weeks"

**✓ shipped.** `_clickTrendInsight()` now scans backwards from the end of
the series and picks the **last two indices where `is_current` is false**.
The percentage and absolute values are computed from those, and the caption
explicitly says "(last 2 completed weeks)" / "(last 2 completed months)" so
it's clear what's being compared. The in-progress bar is still rendered
in the chart (slightly faded) but is excluded from the headline.

### "Remove this part it's already in another section" — Clicks by Category / Author / Tag table

**✓ shipped.** Removed the **Clicks by Category**, **Clicks by Author**,
and **Tag / Topic Performance** blocks from the Click Analysis tab. Those
breakdowns are already covered by the Content Reference tab (Top Categories
and Top Tags charts, plus the Author filter on the article table and the
Sleeper Hits insight). The lambda still publishes `category_performance`,
`author_performance`, and `tag_performance` for backwards-compat, but the
dashboard no longer renders them in Click Analysis.

### "All of these filters return no data except All, so just remove them and make only top 10 by unique clicks"

**✓ shipped.** The **Top Articles by Unique Clicks** card now renders
exactly the **top 10** rows of `M.top_articles` (already sorted by
`unique_clicks` DESC by Q13). The All / 7d / 15d / 30d / 90d window
selector and the `_setArtWindow` plumbing were removed. The lambda still
emits `top_articles_windowed` for backwards-compat (in case the windowed
data starts arriving later), but the dashboard ignores it.
