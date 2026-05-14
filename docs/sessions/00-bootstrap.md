# Session 0: Bootstrap & Scaffold

**Type:** Bootstrap

**Tasks:**

- [ ] Split sessions into individual files. Create `docs/sessions/` directory. For each session defined in `TECHNICAL_PLAN.md` (including this one), create a file named `docs/sessions/NN-slug.md` containing that session's full definition including the Context section. Copy verbatim — do not summarize.
- [ ] Verify toolchain:
  - `python --version` (3.12+)
  - Create `.venv` and activate
  - `pip install pyside6 httpx pydantic structlog pyyaml pytest ruff`
  - `pyside6-designer --version` or equivalent (confirm PySide6 installed)
  - `ruff --version`
  - `pytest --version`
- [ ] Project scaffolding:
  - Create `pyproject.toml` with all dependencies and project metadata
  - Create `src/romulus/__init__.py`, `__main__.py` (minimal entry point that prints "Romulus v0.1.0")
  - Create directory structure per CLAUDE.md Project Structure
  - Create `data/dats/` directory (empty for now — DATs added in Session 3)
  - Create `data/profiles/` directory (empty for now — profiles added in Session 10)
  - Create `.gitignore` (Python defaults + .venv + __pycache__ + .romulus/)
- [ ] Verify build and lint pass:
  - `python -m romulus` runs and prints version
  - `pytest` passes (no tests yet, zero errors)
  - `ruff check src/ tests/` clean
- [ ] Git: initial commit with all scaffolding

**Acceptance criteria:**
- All session files exist in `docs/sessions/` with correct content
- Project runs `python -m romulus` and prints version
- pytest and ruff pass clean
- `.venv` created with all dependencies installed

STOP. Tell me Session 0 is complete. Do not proceed to Session 1.
