# AI Stock Agent — Claude Code Transfer Package

Copy the contents of this package into:

`C:\AI_Stock_Agent`

Final paths should include:

- `CLAUDE.md`
- `docs\PROJECT_CONTEXT.md`
- `docs\CURRENT_STATE.md`
- `docs\DECISIONS_LOG.md`
- `docs\FAILED_APPROACHES.md`
- `docs\WORKFLOW.md`
- `archive\PROJECT_CHAT_HISTORY_SUMMARY.md`
- `.claude\skills\council\SKILL.md`

Do not place them under `.venv`.

## First prompt to Claude Code

```text
קרא את CLAUDE.md ואת כל הקבצים בתיקיות docs ו-archive.
לא לשנות שום קובץ ולא להריץ שום פקודה.
לאחר הקריאה, ענה בעברית על חמש נקודות בלבד:
1. מה מטרת הפרויקט?
2. מהו מקור האמת לנתונים?
3. אילו גישות נפסלו ואסור לחזור אליהן?
4. מהו המצב הטכני הנוכחי?
5. מהו הצעד הבא היחיד שאתה ממליץ לבצע?
```
