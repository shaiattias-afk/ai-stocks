# DERIVED METRICS V1 FROZEN — final read-only verification: PASS

Read-only final verification of the executed
`scripts/153_derived_metrics_v1_load.py --execute` production load. No
database was modified, no extraction or regression was run, no formula
was changed. All 17 required checks were independently re-derived
directly from the live database (not merely taken from the load
script's own self-report).

## Result: PASS — all 17 checks confirmed

| # | Check | Result |
|---|---|---|
| 1 | Load status | `PASS` (`data/derived_metrics_v1_load_result.json`) |
| 2 | `derived_metric_results` exists | ✓ |
| 3 | Total rows = 405 | ✓ |
| 4 | Annual rows = 81 | ✓ |
| 5 | Quarterly rows = 324 | ✓ |
| 6 | Exactly 9 tickers | ✓ (AMZN, CRWD, GOOGL, META, MSFT, MU, NVDA, ORCL, PANW) |
| 7 | No duplicate rows | ✓ 0 duplicate primary keys |
| 8 | No missing required values | ✓ 0 NULLs across all 15 `NOT NULL` columns |
| 9 | Annual rows have no quarter number | ✓ 0 annual rows with non-NULL `fiscal_quarter` |
| 10 | Quarterly rows have quarter 1–4 | ✓ 0 quarterly rows outside 1–4 or NULL |
| 11 | `operating_margin` counts correct | ✓ annual=45, quarterly=180 |
| 12 | `revenue_yoy_growth` counts correct | ✓ annual=36, quarterly=144 |
| 13 | Annual Data V1 checksum unchanged | ✓ `e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814` |
| 14 | `quarterly_extraction_runs` = 45 | ✓ |
| 15 | `quarterly_metric_results` = 1,080 | ✓ |
| 16 | `financial_metric_results` = 900 | ✓ |
| 17 | unique REVIEW_REQUIRED = 0 | ✓ |

**Additional confirmation**: only the 2 approved `derived_metric` values
(`operating_margin`, `revenue_yoy_growth`) are present anywhere in the
table — no extra metric slipped in.

## What was read (all read-only)

- `data/derived_metrics_v1_load_result.json` — `status: PASS`, `rows_inserted: 405`, 9 tickers, all `post_checks` true
- `data/derived_metrics_v1_release_manifest.json` — pre/post load counts identical, `annual_v1_checksum_unchanged: true`
- `data/database/ai_stock_agent.duckdb` — re-queried directly, read-only, for every one of the 17 checks above plus the metric-value and distribution checks

## Derived Metrics V1 is now FROZEN (D-043)

Recorded as a new standing decision in `docs/DECISIONS_LOG.md`:

- **Derived Metrics V1 is frozen.**
- **405 approved derived observations** (81 annual + 324 quarterly) across all 9 approved tickers.
- **Exactly 2 approved metrics: `operating_margin` and `revenue_yoy_growth`.**
- **No future changes (new metrics, formula changes, or data reloads) are permitted without a new version and full validation** — the same discipline already applied to Annual Data V1 and Quarterly Data V1 (D-042).

`docs/CURRENT_STATE.md` updated with the "Last updated" line and a new
dated section recording the full verification table above and the
project's current overall state: **Annual Data V1, Quarterly Data V1,
and Derived Metrics V1 are all now frozen.**

## Files created / updated
- `docs/CURRENT_STATE.md` — updated ("Last updated" line + new dated section)
- `docs/DECISIONS_LOG.md` — new entry D-043
- `docs/LAST_CLAUDE_REPORT.md` — this file

No database was modified by this verification task. No extraction or
regression was run. No formula was changed or metric added.

## Result: PASS

## Git

Commit and tag to follow, covering only:
`data/derived_metrics_v1_load_result.json`,
`data/derived_metrics_v1_release_manifest.json`,
`docs/CURRENT_STATE.md`, `docs/DECISIONS_LOG.md`,
`docs/LAST_CLAUDE_REPORT.md` — no `.duckdb` database, backup, log, PID,
or temporary file.
