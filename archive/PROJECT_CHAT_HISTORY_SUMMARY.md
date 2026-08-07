# AI Stock Agent — Detailed Conversation History Summary

This is a structured project history, not a verbatim export.

## Initial concept
A long-term personal stock-analysis platform with watchlist, scoring, entry-price calculation, historical snapshots, Telegram alerts, and backtesting.

## Early infrastructure
Google Sheets → Google Apps Script → Telegram was previously proven. Python became justified for wider data processing and backtesting.

## Microsoft prototype
Built revenue, growth, balance-sheet, cash-flow, FCF, NOPAT, ROIC, price-return, and backtest components.

## 10-K source decision
The user chose the official 10-K as primary. Company Facts became QA only.

## Meta HTML investigation
Multiple candidate tables caused repeated diagnostics. This exposed that HTML layout was the wrong semantic layer.

## Inline XBRL pivot
Meta and Microsoft core income metrics passed after fact extraction and technical deduplication. Oracle exposed `NetIncomeLoss` versus `ProfitLoss` and later exposed the failure of manual revenue tag lists.

## Council architecture decision
Selected the Accession-Locked Arelle Statement Pipeline:
exact filing → full DTS → presentation/calculation structure → canonical metrics → QA → fail closed.

## Arelle
Installed `arelle-release 2.43.1`; import returned `ARELLE_OK`.

## Filing lock
An early test loaded Oracle 2023 incorrectly. A new downloader locked by exact report date and accession. Generic SEC User-Agent received 403; `Shai Attias shaiattias@gmail.com` succeeded.

Oracle fiscal 2024:
- directory `data\sec_filings_locked\ORCL\000095017024075605`
- primary document `orcl-20240531.htm`

## Locked Arelle tests
Offline presentation output did not show Revenue/Sales candidates. An online run hung and produced an empty log. A bounded test was prepared but not yet verified.

## Claude Code migration
The official Anthropic extension was installed and authenticated to reduce manual copying, wrong file locations, terminal mistakes, and loss of context.
