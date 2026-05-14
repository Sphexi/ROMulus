# Session 8: Review & Docs Sync

**Type:** Review

**Covers:** Sessions 5–7

**Tasks:**

- [ ] Read completion summaries from `docs/sessions/05-ui-shell.md`, `06-metadata.md`, `07-detail-panel.md`
- [ ] Code review all code changed in Sessions 5–7:
  - UI thread safety: are all SQLite calls happening in workers, not the main thread? (Read-only queries for display are OK on main thread for small result sets)
  - Signal/slot connections: any disconnected signals or missing connections?
  - Memory management: are QThread workers properly cleaned up?
  - Error handling in metadata clients: network errors, malformed responses, missing fields
  - httpx usage: are connections being closed properly? Using context managers?
  - Cover cache: is the cache directory created if missing? Are file writes atomic?
- [ ] Security review:
  - ScreenScraper credentials stored in SQLite — is the DB file-permission restricted?
  - Any user input flowing into URLs without sanitization?
  - Any file paths from user input used without validation?
- [ ] Fix any findings, re-run `pytest && ruff check src/ tests/`
- [ ] Update documentation if completion summaries flagged changes

**Acceptance criteria:**
- All review findings addressed
- All tests pass after fixes
- ruff clean

STOP. Commit with message "Session 8: Review sessions 5-7". Do not proceed to Session 9.
