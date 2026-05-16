"""SQLite connection management.

Romulus stores everything in a single SQLite database. The on-disk location
depends on how the app was launched — see :func:`romulus.app.resolve_data_dir`
for the precedence rules (``ROMULUS_DATA_DIR`` env > ``<install_dir>/data/``
> ``~/.romulus/``). To avoid importing the Qt-heavy ``romulus.app`` from this
module the same resolution logic is duplicated here in trimmed-down form.

The connection is configured with WAL mode (for safer concurrent reads while
background workers are writing) and foreign keys (off by default in sqlite3).

The DB also stores ScreenScraper credentials in plaintext (no key management
in the app), so on POSIX we restrict the file permissions to 0o600 to keep
other users on the same machine from reading them. NTFS inherits ACLs from
the parent directory; we do not attempt to tighten Windows ACLs here.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: Env var that lets a user pin the data directory regardless of install
#: location. Mirrors :data:`romulus.app.DATA_DIR_ENV_VAR` — defined here too
#: so this module stays importable before ``romulus.app``.
_DATA_DIR_ENV_VAR: str = "ROMULUS_DATA_DIR"
_LEGACY_DATA_DIR: Path = Path.home() / ".romulus"


def _resolve_install_dir() -> Path:
    """Frozen-exe parent dir, or the repo root in a dev clone."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    cursor = Path(__file__).resolve()
    for parent in cursor.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return _LEGACY_DATA_DIR


def _is_writable_dir(path: Path) -> bool:
    """Probe-file writability check shared with :func:`romulus.app.resolve_data_dir`."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = path / ".romulus_write_probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError:
        return False
    return True


def _resolve_data_dir() -> Path:
    """Mirror of :func:`romulus.app.resolve_data_dir` for early-import use."""
    override = os.environ.get(_DATA_DIR_ENV_VAR, "").strip()
    if override:
        chosen = Path(override).expanduser()
        chosen.mkdir(parents=True, exist_ok=True)
        return chosen
    install_data = _resolve_install_dir() / "data"
    if _is_writable_dir(install_data):
        return install_data
    _LEGACY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _LEGACY_DATA_DIR


def _current_default_db_path() -> Path:
    """Return the runtime-resolved default DB path.

    Re-evaluated per call so a ``ROMULUS_DATA_DIR`` set after import still
    takes effect (e.g. inside tests that monkeypatch the env var).
    """
    return _resolve_data_dir() / "romulus.db"


# ``DEFAULT_DB_PATH`` is exposed as a module attribute for backward
# compatibility (workers and tests grab it directly), but resolved lazily so
# the env-var override is honored even when this module is imported very
# early. ``__getattr__`` is PEP 562 — invoked only on misses.


def __getattr__(name: str) -> object:
    if name == "DEFAULT_DB_PATH":
        return _current_default_db_path()
    if name == "DEFAULT_DB_DIR":
        return _resolve_data_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _restrict_db_permissions(db_path: Path) -> None:
    """On POSIX, chmod the DB (and its WAL/SHM siblings) to owner-only.

    No-op on Windows: NTFS ACLs are inherited from the parent directory and
    `os.chmod` only toggles the read-only bit, which would actively prevent
    sqlite from writing. Failures are logged but not raised — restrictive
    permissions are defense-in-depth, not a hard precondition.
    """
    if sys.platform == "win32":
        return
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path.with_name(db_path.name + suffix)
        if not candidate.exists():
            continue
        try:
            os.chmod(candidate, 0o600)
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            logger.debug("could not chmod %s: %s", candidate, exc)


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and configure) a SQLite connection.

    If `db_path` is omitted, uses :func:`_current_default_db_path` (which
    honors ``ROMULUS_DATA_DIR`` > install-dir > legacy ``~/.romulus``). Always
    enables WAL mode and foreign-key enforcement. ``row_factory`` is set to
    ``sqlite3.Row`` so callers can access columns by name. On POSIX systems
    the DB file is also chmod'd to 0o600 (owner-only) because it stores
    ScreenScraper credentials in plaintext.
    """
    if db_path is None:
        db_path = _current_default_db_path()
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _restrict_db_permissions(Path(db_path))
    return conn
