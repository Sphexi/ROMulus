# Session 4: Review & Docs Sync

**Type:** Review

**Covers:** Sessions 1–3

**Tasks:**

- [ ] Read completion summaries from `docs/sessions/01-data-models.md`, `02-scanner.md`, `03-identifier.md`
- [ ] Code review all code changed in Sessions 1–3:
  - Check for error handling gaps (file I/O, corrupt files, missing directories)
  - Check for unused imports, dead code
  - Check type hints coverage on all public functions
  - Check docstrings on all public classes/methods
  - Check SQL injection risk (all queries should use parameterized placeholders)
  - Check thread safety of SQLite access (connections not shared across threads)
- [ ] Security review:
  - Path traversal risk in scanner (does it stay within library_path?)
  - SQL injection in query parameters
  - File permission handling during scan
- [ ] Fix any findings, re-run `pytest && ruff check src/ tests/`
- [ ] Update any documentation that completion summaries flagged

**Acceptance criteria:**
- All review findings addressed
- All tests pass after fixes
- ruff clean

STOP. Commit with message "Session 4: Review sessions 1-3". Do not proceed to Session 5.
