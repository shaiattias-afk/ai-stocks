# AI Stock Agent — Failed or Superseded Approaches

## 1. HTML-table-first extraction
Tried locating Financial Statements, detecting Notes boundaries, comparing candidate tables, and profiling surrounding text. Repeated tables, footnotes, references, and layout artefacts made this fragile.

**Rule:** Do not return to HTML table parsing as the primary method.

## 2. Company-specific scripts
Special handling for Meta or Oracle does not scale.

**Rule:** Final-path scripts should accept ticker/report date/accession-related inputs.

## 3. Manual tag-list mapping as the universal engine
Worked for Meta and Microsoft and partly for Oracle, but failed on Oracle 2024 revenue.

**Rule:** Keep as QA baseline only.

## 4. Selecting source files by size or filename year
Loaded `orcl-20230531.htm` while targeting fiscal 2024.

**Rule:** Use exact report date and accession.

## 5. Unbounded Arelle online execution
Hung without useful logs and required force termination.

**Rule:** Use connection timeout, total timeout, observable output, summary file, and deterministic termination.

## 6. Assuming why presentation rows were missing
Missing external taxonomies was suggested but not proven.

**Rule:** Treat as a hypothesis until schema references or model diagnostics confirm it.

## 7. Recommending tools before checking fit
Make and an outdated FMP endpoint were previously suggested too early.

**Rule:** Test exact current capability and plan first.
