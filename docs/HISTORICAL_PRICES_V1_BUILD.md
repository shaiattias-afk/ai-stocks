# Historical Prices V1 — production loader build — RESULT: PASS (check-only)

Built the production loader for the 9-approved-company historical
price dataset, applying Historical Price Policy V1 / D-044
(`docs/PRICE_POLICY_V1.md`) exactly. **Only `--check-only` was run.
`--execute` was never invoked — the production database was not
modified.**

Source code: `scripts/158_historical_prices_v1_load.py`.
Outputs: `data/historical_prices_v1_build_validation.json`,
`data/historical_prices_v1_preview.csv`.

## What this script does

Rebuilds the complete 9-company, 14,913-row dataset **from scratch**
by re-parsing the already-saved raw Yahoo JSON responses (the exact
files identified as authoritative by
`data/proofs/9_ticker_historical_price_proof.json`) — never reads the
previously-computed proof CSV as a shortcut, never downloads anything
new. For every row it computes, per Rule C of D-044, the reconstructed
nominal `open`/`high`/`low`/`close` (Yahoo OHLC × product of every
split ratio whose effective date is strictly after that price date).

## Command run

```
.\.venv\Scripts\python.exe .\scripts\158_historical_prices_v1_load.py --check-only
```

`--execute` exists in the script (built fully, matching the project's
established production-loader pattern) but was never run.

## Future production table

```sql
CREATE TABLE historical_prices_daily (
    ticker                  VARCHAR NOT NULL,
    price_date              DATE NOT NULL,
    open                    DOUBLE NOT NULL,
    high                    DOUBLE NOT NULL,
    low                     DOUBLE NOT NULL,
    close                   DOUBLE NOT NULL,
    adj_close               DOUBLE NOT NULL,
    nominal_open            DOUBLE NOT NULL,
    nominal_high            DOUBLE NOT NULL,
    nominal_low             DOUBLE NOT NULL,
    nominal_close           DOUBLE NOT NULL,
    volume                  BIGINT NOT NULL,
    dividend                DOUBLE,
    split_ratio             VARCHAR,
    source                  VARCHAR NOT NULL,
    source_raw_file         VARCHAR NOT NULL,
    source_raw_sha256       VARCHAR NOT NULL,
    price_policy_version    VARCHAR NOT NULL,
    created_at              TIMESTAMP NOT NULL,
    CONSTRAINT chk_prices_positive CHECK (...),
    CONSTRAINT chk_volume_non_negative CHECK (volume >= 0),
    CONSTRAINT chk_ohlc_valid CHECK (...),
    CONSTRAINT chk_nominal_ohlc_valid CHECK (...),
    CONSTRAINT chk_price_policy_version CHECK (price_policy_version = 'HISTORICAL_PRICE_POLICY_V1'),
    PRIMARY KEY (ticker, price_date)
)
```

`price_policy_version = 'HISTORICAL_PRICE_POLICY_V1'` on every row.
`dividend`/`split_ratio` are nullable (most trading days have neither);
every other column is `NOT NULL`. Since none of the `NOT NULL` columns
are ever `NULL`, the `CHECK` constraints use plain comparisons — no
`NULL`-vs-`BETWEEN` three-valued-logic pitfall (the class of bug found
and fixed during the Derived Metrics V1 build, D-043) applies here.

## Validation results — all PASS

| Check | Result |
|---|---|
| Exactly 9 tickers | ✓ |
| No unexpected ticker | ✓ |
| Exactly 1,657 rows per ticker | ✓ (all 9) |
| Exactly 14,913 total rows | ✓ |
| Dates unique per ticker, no duplicate ticker/date rows | ✓ |
| No missing OHLC/adjusted-close values | ✓ |
| No negative prices | ✓ |
| No negative volume | ✓ |
| All OHLC relationships valid | ✓ |
| All reconstructed nominal prices positive | ✓ |
| Reconstructed OHLC relationships valid | ✓ |
| Every raw source file exists | ✓ (all 9) |
| Every raw source file SHA-256 recorded | ✓ (all 9) |
| All split events match the existing 9-company proof | ✓ |
| All dividend counts match the existing 9-company proof | ✓ |
| NVDA/GOOGL/PANW reconstructed results match Historical Price Policy V1 proof | ✓ (35 dates cross-checked, 0 mismatches) |
| No database modification | ✓ (SHA-256 before/after identical) |

## Per-ticker summary

| Ticker | Rows | First date | Last date | Clean |
|---|---:|---|---|---|
| ORCL | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| MSFT | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| META | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| NVDA | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| GOOGL | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| AMZN | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| MU | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| CRWD | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |
| PANW | 1,657 | 2020-01-02 | 2026-08-06 | ✓ |

## In-memory load proof

Before `--check-only` finished, the exact future production schema
(DDL above) was created in a `:memory:` DuckDB connection (no file
database touched), all 14,913 rows were inserted inside one explicit
transaction, and `COMMIT` was proven to succeed:

- Rows inserted: **14,913**
- Distinct tickers: **9**
- Rows per ticker: **1,657 each** (all 9 confirmed)
- Duplicate `(ticker, price_date)` keys: **0**
- `NULL` values in any required field: **0**
- Rows with wrong `price_policy_version`: **0**
- Commit succeeded: **True**
- File database touched: **False**

## Cross-checks against prior proofs

- **Splits**: re-parsed splits for all 9 tickers, from the raw JSON,
  matched exactly against `data/proofs/9_ticker_historical_price_proof.json`'s
  recorded splits — 0 mismatches.
- **Dividends**: re-parsed dividend counts for all 9 tickers matched
  exactly against the same prior proof — 0 mismatches.
- **Historical Price Policy V1 proof**: for NVDA, GOOGL, and PANW, the
  reconstructed `nominal_close` was recomputed independently (from raw
  JSON, not from the proof's own CSV) and compared against every date
  in `data/proofs/price_policy_v1_proof.json`'s split-window tables (35
  dates across all 5 known split events) — 0 mismatches, tolerance
  $0.001.

## Production remained untouched

Production database SHA-256 was identical before and after this run.
The `historical_prices_daily` table does not yet exist in production
(confirmed directly). Annual Data V1, Quarterly Data V1, Derived
Metrics V1, and Historical Price Policy V1 were not read or modified.

## Files produced
- `scripts/158_historical_prices_v1_load.py` (new)
- `data/historical_prices_v1_build_validation.json` (new — full validation detail)
- `data/historical_prices_v1_preview.csv` (new — all 14,913 computed rows)
- `docs/HISTORICAL_PRICES_V1_BUILD.md` (this file)
- `docs/LAST_CLAUDE_REPORT.md` — updated

No new market data was downloaded. No database was modified. No
backtest was run.

## Result: PASS (check-only)
Runtime **21.15s** (well under the 5-minute expectation).

## Future manual `--execute` command (not run by this task)

```
.\.venv\Scripts\python.exe .\scripts\158_historical_prices_v1_load.py --execute
```
