# AI Stock Agent — Working Procedure

## Standard loop
1. State one goal.
2. Inspect actual files and output.
3. Separate facts from hypotheses.
4. Recommend one leading solution.
5. Create one new complete script under `scripts`.
6. Show the exact command.
7. Run a small bounded proof.
8. Save CSV/JSON/log output.
9. Verify success criteria.
10. Update `docs/CURRENT_STATE.md`.
11. Continue to the next obvious step.

## Claude Code safety at the start
- Start read-only / plan mode.
- Do not use broad `Always Allow`.
- Do not enable bypass permissions.
- Ask before Terminal commands.
- Show files to be changed.
- Review diffs.
- Change one subject at a time.
- Never expose secrets.
- Do not use API billing without explicit approval.

## Status labels
Use `PASS`, `REVIEW_REQUIRED`, `FAIL`, and `TIMEOUT`.

## Required lineage
Retain ticker, CIK, form, accession, report date, filing date, primary document, statement role, source concept, label, context, periods, unit, dimensions, value, and validation status.

## Backtesting gate
Test point-in-time availability, predictive value, factor correlation, weight sensitivity, out-of-sample performance, regimes, benchmarks, drawdown, and risk.

## Communication style
Use:
- Goal
- Create file
- Run command
- Expected output
