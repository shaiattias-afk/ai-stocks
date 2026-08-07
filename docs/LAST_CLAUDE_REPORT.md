# Historical Prices V1 — production loader build — RESULT: PASS (check-only)

Built the production loader (`scripts/158_historical_prices_v1_load.py`)
for the 9-company historical price dataset, applying Historical Price
Policy V1 / D-044 exactly (Rule A: preserve Yahoo's original fields;
Rule C: reconstruct nominal historical OHLC). **Only `--check-only` was
run — `--execute` was never invoked. The production database was not
modified.**

Full report: `docs/HISTORICAL_PRICES_V1_BUILD.md`.
Outputs: `data/historical_prices_v1_build_validation.json`,
`data/historical_prices_v1_preview.csv` (14,913 rows).

## Command run

```
.\.venv\Scripts\python.exe .\scripts\158_historical_prices_v1_load.py --check-only
```

## Result summary

- **9 companies**, **14,913 daily price rows** (1,657 per ticker),
  **2020-01-02 to 2026-08-06** — rebuilt from scratch by re-parsing the
  already-saved raw Yahoo JSON files, never from the prior proof's own
  CSV output.
- **All validation checks passed**: exactly 9 tickers, no unexpected
  ticker, exactly 1,657 rows/ticker, exactly 14,913 total, no
  duplicates, no missing/negative values, all OHLC and reconstructed
  nominal-OHLC relationships valid, every raw source file exists with
  its SHA-256 recorded, all split events and dividend counts matched
  the existing 9-company proof exactly, and NVDA/GOOGL/PANW's
  reconstructed nominal prices matched the Historical Price Policy V1
  proof exactly (35 dates checked, 0 mismatches).
- **In-memory load proof passed**: the exact future production schema
  was created in a `:memory:` DuckDB connection (no file touched), all
  14,913 rows inserted in one transaction, commit succeeded — 9
  tickers, 1,657 rows each, 0 duplicate keys, 0 NULLs in any required
  field.
- **Production remained untouched**: database SHA-256 identical
  before/after; `historical_prices_daily` does not yet exist in
  production.

## Future production table

`historical_prices_daily` — one row per ticker per trading date, PK
`(ticker, price_date)`, columns per the task spec plus 4 `CHECK`
constraints (positive prices, non-negative volume, valid OHLC, valid
reconstructed nominal OHLC, correct policy version tag).
`price_policy_version = 'HISTORICAL_PRICE_POLICY_V1'` on every row.

## Files created
- `scripts/158_historical_prices_v1_load.py` (new)
- `data/historical_prices_v1_build_validation.json` (new)
- `data/historical_prices_v1_preview.csv` (new)
- `docs/HISTORICAL_PRICES_V1_BUILD.md` (new)
- `docs/LAST_CLAUDE_REPORT.md` — this file, updated

No existing database was modified. No new market data was downloaded.
No backtest was run. Annual Data V1, Quarterly Data V1, Derived
Metrics V1, and Historical Price Policy V1 (D-044) were untouched.

## Result: PASS
Runtime **21.15s** (well under the 5-minute expectation).

## Report — in simple terms

- **Number of companies?** 9.
- **Number of daily price rows?** 14,913 (1,657 per company).
- **Date range?** 2020-01-02 to 2026-08-06.
- **Did all validation pass?** Yes — every check listed in the task
  passed, including cross-checks against both prior proofs.
- **Did the in-memory load simulation pass?** Yes — 14,913 rows
  inserted and committed against the exact future production schema,
  entirely in memory, no file database touched.
- **Did production remain untouched?** Yes — database checksum
  unchanged, target table does not yet exist.
- **Exact Git commit hash?** See below (committed after this report).
- **Exact future manual `--execute` command?**
  `.\.venv\Scripts\python.exe .\scripts\158_historical_prices_v1_load.py --execute`
