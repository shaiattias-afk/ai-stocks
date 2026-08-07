---
name: council
description: Run the full five-expert council process for major AI Stock Agent decisions.
---

# Council Process

## Trigger
If the user’s entire message is exactly `מועצה` and no question has been provided, reply only:

`מה השאלה?`

When the question arrives, run the full process.

## Stage 1 — Five independent answers
Use five separated expert perspectives, preferably separate subagents when available:

1. XBRL / SEC data architect
2. Financial accountant / valuation analyst
3. Data engineer / Python systems engineer
4. Backtesting and model-risk expert
5. Product and implementation lead

Label them anonymously:
יועץ א׳, יועץ ב׳, יועץ ג׳, יועץ ד׳, יועץ ה׳.

Each must give a complete answer, separate facts/assumptions/risks/recommendation, propose one practical path, and consider measurement and backtesting.

## Stage 2 — Anonymous peer review
Each advisor reviews the other four anonymously, states strengths and weaknesses, assigns 0–10 scores, and explains briefly. Calculate averages accurately. Do not reveal identities yet.

## Stage 3 — Reveal identities
Reveal roles only after reviews and averages.

## Stage 4 — Chair decision
Give:
1. one leading decision;
2. why selected;
3. what is rejected;
4. risks and safeguards;
5. one immediate next step;
6. a success criterion.

Do not end with a long menu.

## Integrity
- Do not fabricate real external experts or consensus.
- This is a structured multi-perspective reasoning process.
- Verify current factual claims from official sources when tools are available.
- If separate subagents are unavailable, state that perspectives were procedurally separated.
- Majority vote is not proof.
