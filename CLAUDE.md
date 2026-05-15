# CLAUDE.md — Romulus

## What This Project Is

Romulus is a local-first desktop ROM collection manager for retro game consoles. It scans, identifies, enriches with metadata/cover art, organizes, and exports ROM collections to device-specific folder structures (Anbernic, Batocera, MiSTer, etc.). Built with Python + PySide6 (Qt), SQLite, and no server infrastructure.

## Project Tier

**Standard** — unit tests + ruff, code reviews every 2–3 build sessions.

## Session Start

At the start of every session:
1. Read this file for project rules and architecture
2. Read the current session file from `docs/sessions/` (check `docs/TECHNICAL_PLAN.md` session overview table for the current session number, then open `docs/sessions/NN-slug.md`)
3. Produce an execution plan before writing any code

**Find sessions:** `ls docs/sessions/`

## Follow the Plan

Claude Code MUST follow the tasks in the current session file. Do not add features, refactors, or improvements not specified. Do not ask questions already answered in CLAUDE.md or the session file's Context section. If something seems missing, flag it — do not silently add unplanned work.

## Reference Documents

| Document | Purpose | When to Read |
|---|---|---|
| `docs/sessions/NN-slug.md` | Current session tasks, context, acceptance criteria | Session start (self-contained — has everything you need) |
| `docs/TECHNICAL_PLAN.md` | Full API details, schema, implementation pseudocode | On-demand for edge cases not covered in the session file |
| `docs/ROM-FORMATS-REFERENCE.md` | Extension tables, naming conventions, folder aliases | When implementing scanner or system registry |
| `docs/ROM-DEDUP-METHODOLOGY.md` | Three-layer identification pipeline methodology | When implementing identifier pipeline |
| `docs/ROM-LIBRARY-ANALYSIS-REPORT.md` | Real-world library stats, test validation data | When writing tests or validating assumptions |

Do not load reference documents into context every turn — read them when needed.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     PySide6 UI                           │
│  ┌─────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ System   │  │  Game Table  │  │   Game Detail Panel │ │
│  │ Sidebar  │  │  (sortable,  │  │   (cover, desc,     │ │
│  │          │  │   filterable)│  │    metadata, tags)   │ │
│  └─────────┘  └──────────────┘  └─────────────────────┘ │
│  ┌──────────────────────────────────────────────────────┐│
│  │ Toolbar: Quick Scan | Heavy Scan | Organize |        ││
│  │          Enrich | Export | Settings                   ││
│  └──────────────────────────────────────────────────────┘│
└──────────────┬───────────────────────────────────────────┘
               │ signals/slots + QThread workers
┌──────────────┴───────────────────────────────────────────┐
│                    Core Engine                            │
│                                                           │
│  Scanner ──→ Identifier Pipeline ──→ SQLite DB            │
│              (L1 fuzzy, L2 header,     │                  │
│               L3 hash+DAT)        Metadata Client         │
│                                   (libretro-thumbnails,   │
│  DAT Parser (bundled + user)       Hasheous, LaunchBox)   │
│                                                           │
│  Organizer (rename/merge/dedup     Export Engine           │
│             with preview/commit)   (dest profiles, copy,  │
│                                    gamelist.xml, .lpl)     │
│                                                           │
│  Cover Cache (~/.romulus/covers/)                          │
│  SQLite DB   (~/.romulus/romulus.db)                       │
└───────────────────────────────────────────────────────────┘
```

**Key architecture notes:**
- Single-process desktop app, no server, no Docker
- SQLite for all persistent state (library, config, metadata, scan history)
- QThread workers for scanner/hasher/enricher with progress signals to UI
- Quick scan (L1+L2, seconds-to-minutes) vs Heavy scan (L3, minutes-to-hours)
- Config stored in SQLite, not files — user edits everything via Settings dialog

## Tech Stack

| Concern | Choice |
|---|---|
| **Language** | Python 3.12+ |
| **GUI** | PySide6 (Qt 6) |
| **HTTP client** | httpx (async for metadata fetching) |
| **Database** | SQLite via sqlite3 stdlib (no ORM) |
| **Config/models** | Pydantic v2 for data models, validation |
| **Logging** | structlog, JSON to stdout |
| **Linting** | ruff |
| **Testing** | pytest |
| **Packaging** | pyproject.toml, .venv |

## Project Structure

```
romulus/
├── CLAUDE.md
├── pyproject.toml
├── .claude/
│   └── settings.json
├── data/
│   └── dats/                    # Bundled No-Intro DAT files (~30 systems)
│   └── profiles/                # Built-in destination profiles (YAML)
├── docs/
│   ├── TECHNICAL_PLAN.md
│   ├── ROM-FORMATS-REFERENCE.md
│   ├── ROM-DEDUP-METHODOLOGY.md
│   ├── ROM-LIBRARY-ANALYSIS-REPORT.md
│   └── sessions/
│       ├── 00-bootstrap.md
│       ├── 01-data-models.md
│       ├── ...
│       └── 11-final-review.md
├── src/
│   └── romulus/
│       ├── __init__.py
│       ├── __main__.py          # Entry point
│       ├── app.py               # QApplication setup
│       ├── db/
│       │   ├── __init__.py
│       │   ├── connection.py    # SQLite connection manager
│       │   ├── schema.py        # Table definitions, migrations
│       │   └── queries.py       # Query functions
│       ├── core/
│       │   ├── __init__.py
│       │   ├── scanner.py       # Filesystem walker, platform detection
│       │   ├── identifier.py    # Three-layer identification pipeline
│       │   ├── hasher.py        # SHA-1/CRC32 with header stripping
│       │   ├── dat_parser.py    # Logiqx XML DAT parser
│       │   ├── organizer.py     # Library reorganization (preview/commit)
│       │   └── exporter.py      # Destination profile export engine
│       ├── metadata/
│       │   ├── __init__.py
│       │   ├── libretro.py      # libretro-thumbnails cover art
│       │   ├── hasheous.py      # Hasheous API client
│       │   ├── launchbox.py     # LaunchBox XML parser
│       │   └── screenscraper.py # ScreenScraper API client (optional)
│       ├── models/
│       │   ├── __init__.py
│       │   ├── system.py        # System/platform definitions
│       │   ├── rom.py           # ROM file model
│       │   ├── game.py          # Logical game model
│       │   └── profile.py       # Destination profile model
│       └── ui/
│           ├── __init__.py
│           ├── main_window.py   # Main window layout
│           ├── system_sidebar.py
│           ├── game_table.py
│           ├── detail_panel.py
│           ├── settings_dialog.py
│           ├── scan_progress.py
│           ├── organize_preview.py
│           ├── export_dialog.py
│           └── workers.py       # QThread workers for async ops
└── tests/
    ├── conftest.py
    ├── test_scanner.py
    ├── test_identifier.py
    ├── test_hasher.py
    ├── test_dat_parser.py
    ├── test_organizer.py
    ├── test_exporter.py
    └── test_metadata.py
```

## Git Policy

Claude Code handles `git add` and `git commit` at the end of each session. `git push` is ALWAYS denied. `git merge`, `git rebase`, `git stash`, `git reset --hard` are denied.

At the end of each session, stage and commit all changes with a descriptive commit message referencing the session number.

## Code Style & Conventions

- Python 3.12+ — modern type hints (`str | None`), match statements
- Type hints on every function signature
- Docstrings on every public class/method/function
- Pydantic v2 for all data models crossing boundaries
- structlog for structured JSON logging to stdout
- httpx as the only HTTP client
- Virtual env in `.venv` at project root
- No global state — pass dependencies explicitly
- SQL queries as plain strings in `db/queries.py`, not scattered across modules
- Constants in UPPER_SNAKE_CASE at module level
- Private methods prefixed with underscore
- `from romulus.db import queries as q` — alias `q.` is the preferred form in long files; bare `queries.` is acceptable inside `db/__init__.py` and its near neighbours

## Key Design Rules (Non-Negotiable)

1. **Local-first.** No server, no Docker, no external dependencies to run. SQLite for storage, files on disk for covers.
2. **No external CDN/JS dependencies.** All assets vendored locally if needed.
3. **Quick scan must be fast.** L1 (fuzzy filename) + L2 (internal header) run automatically during scan. L3 (hash+DAT) is a separate "Heavy Scan" action with a progress dialog and duration warning.
4. **Never modify files without preview.** The Organizer shows a before/after diff. The Exporter shows what will be copied. User must explicitly confirm before any filesystem changes.
5. **Hacks are first-class artifacts.** Never silently deduplicate a hack against its original. Treat them as distinct titles.
6. **Hash cache is sacred.** Hashes are expensive. Cache in SQLite keyed by (path, mtime, size). Reuse on rescan if file hasn't changed.
7. **DATs are bundled.** No-Intro DATs for ~30 common systems ship in `data/dats/`. Users can add more to a configurable folder. The app watches both.
8. **Cover art is free.** Primary source: libretro-thumbnails (HTTP, no API key). ScreenScraper is optional — app prompts user, works without it.
9. **Config lives in SQLite.** No manual config file editing. Everything through the Settings dialog.
10. **Destination profiles are YAML.** Ship 6 built-in profiles. Users can create custom ones in `~/.romulus/profiles/`.

## Scan Types

| Type | What runs | Speed | Trigger |
|---|---|---|---|
| **Quick Scan** | Filesystem walk + platform detection + filename parsing (L1) + internal header extraction (L2) | Seconds to minutes | "Quick Scan" button or on library import |
| **Heavy Scan** | SHA-1/CRC32 hashing + DAT matching (L3) | Minutes to hours (240 GB ≈ 80 min over SMB) | "Heavy Scan" button with warning dialog |

Quick scan gives a browsable library immediately. Heavy scan unlocks canonical naming, accurate dedup, cover art matching, and completeness reporting.

## Agent & Plugin Routing

Plugin agents take priority over custom agents when their domain matches.

| Task | Agent/Plugin |
|---|---|
| General Python (async, Pydantic, PySide6) | `python-pro` plugin |
| Backend architecture, data flow | `backend-architect` plugin |
| TDD, test-first development | `tdd-orchestrator` plugin |
| Test automation, pytest suites | `test-automator` plugin |
| Debugging, runtime errors | `debugger` plugin |
| Error diagnosis | `error-detective` plugin |
| Code quality, refactoring | `code-reviewer` plugin |
| Security audit | `security-auditor` plugin |
| Performance optimization | `performance-engineer` plugin |
| Database optimization | `database-optimizer` plugin |
| Database architecture, SQL | `database-architect` / `sql-pro` plugin |
| Architecture docs, tutorials | `docs-architect` / `tutorial-engineer` plugin |
| Project spec, CLAUDE.md, TECHNICAL_PLAN.md | `project-architect` custom |
| Session planning, task breakdown | `task-orchestrator` custom |
| PySide6 UI, Qt widgets | `frontend-engineer` custom |
| Shell scripting (Bash) | `bash-powershell-engineer` custom |
| REST API clients (httpx, metadata APIs) | `rest-api-engineer` custom |
| README, CHANGELOG, docstrings | `docs-writer` custom |

## Agent Callout Format

**Orchestrator — when assigning tasks:**
```
📋 task-orchestrator → Assigning Session N tasks:
  • [agent-name]  : [task description]
  • [agent-name]  : [task description]
  ([agent-a] and [agent-b] can run in parallel; [agent-c] waits for [agent-a])
```

**Each agent — when starting:** `🔧 [agent-name] → Starting: [brief task description]`
**Each agent — when finishing:** `✅ [agent-name] → Done: [what was produced] ([file paths if applicable])`
**Each agent — if blocked:** `⚠️ [agent-name] → Blocked: [reason]. Waiting on: [dependency].`

## CI/CD Local Validation Rule

Whenever a GitHub Actions workflow is created or modified that runs lint or tests — run those exact same commands locally first and resolve all failures before the workflow is committed.

## Completion Summary Template

Every session ends by appending this block to its session file:

```markdown
## Completion Summary
**Status:** COMPLETE
**Date:** {{DATE}}
**What was built/changed:** {{brief summary}}
**Tests:** {{pass/fail summary}}
**Config changes:** {{new settings, or "None"}}
**Breaking changes:** {{list or "None"}}
**Carry-forward notes:** {{anything the next session or review session needs to know}}
```
