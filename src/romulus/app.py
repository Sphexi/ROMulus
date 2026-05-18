"""Application initialization — database setup and main window launch."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog

from romulus.db import (
    create_tables,
    get_config,
    get_connection,
    seed_defaults,
    set_config,
)
from romulus.db import queries as q
from romulus.models import seed_systems

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_VALID_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")

#: Env var that lets a user pin the data directory regardless of where the
#: app is installed. Honored first by :func:`resolve_data_dir`.
DATA_DIR_ENV_VAR: str = "ROMULUS_DATA_DIR"

#: Legacy data directory used by v0.1.0 (and as the last-resort fallback
#: when the install dir isn't writable).
LEGACY_DATA_DIR: Path = Path.home() / ".romulus"

# Third-party loggers whose DEBUG output is rarely useful even when we want
# verbose app logs. ``httpcore`` emits 10+ lines per HTTP request describing
# TCP/TLS internals; ``httpx`` itself stays at INFO and reports the request
# verb + URL + status, which IS useful. Capped at INFO so DEBUG-level ROMULUS
# stays focused on our own code.
_NOISY_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "httpcore",
    "urllib3",
    "asyncio",
    "PIL",
)


def _resolve_install_dir() -> Path:
    """Best-effort: find the directory where ROMulus is installed.

    Three lookup strategies, tried in order:

    1. **PyInstaller-frozen exe** — ``sys.executable``'s parent.
    2. **Editable install / dev clone** — walk up from this module looking
       for ``pyproject.toml``.
    3. **Fallback** — ``~/.romulus`` so logs still land somewhere writable.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    cursor = Path(__file__).resolve()
    for candidate in cursor.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.home() / ".romulus"


INSTALL_DIR = _resolve_install_dir()
DEFAULT_LOG_DIR = INSTALL_DIR / "logs"
DEFAULT_LOG_PATH = DEFAULT_LOG_DIR / "romulus.log"


def _is_writable_dir(path: Path) -> bool:
    """Return True iff ``path`` (or its parent) can accept a new file.

    Used by :func:`resolve_data_dir` to decide whether to use the install
    directory or fall back to the user's home. The check creates and removes
    a probe file rather than trusting ``os.access`` — on Windows the latter
    lies about ACL-restricted directories that mkdir would fail in.
    """
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


def resolve_data_dir() -> Path:
    """Pick where the SQLite DB and cover cache live for this run.

    Resolution order:

    1. ``ROMULUS_DATA_DIR`` env var — explicit user override always wins.
       The directory is created if missing.
    2. ``<install_dir>/data/`` — preferred for portable ZIP installs so the
       user can copy/back-up the whole folder as one unit.
    3. ``~/.romulus/`` — legacy v0.1.0 location; used when the install dir
       isn't writable (read-only mount, system-protected ``Program Files``
       layout, etc.).

    Always creates the chosen directory before returning so callers can
    write to it immediately.
    """
    override = os.environ.get(DATA_DIR_ENV_VAR, "").strip()
    if override:
        chosen = Path(override).expanduser()
        chosen.mkdir(parents=True, exist_ok=True)
        return chosen
    install_data = INSTALL_DIR / "data"
    if _is_writable_dir(install_data):
        return install_data
    LEGACY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return LEGACY_DATA_DIR


def _copy_yaml_dir_if_missing(
    source: Path, dest: Path, label: str
) -> int:
    """Copy every ``*.yaml`` from ``source`` to ``dest`` iff ``dest`` is empty.

    Idempotent — once the user has any file in ``dest`` we keep our hands
    off so user edits survive subsequent launches. Returns the count of
    files copied (0 if nothing changed).
    """
    if not source.is_dir():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    has_existing = any(dest.glob("*.yaml"))
    if has_existing:
        return 0
    copied = 0
    for src in sorted(source.glob("*.yaml")):
        try:
            shutil.copy2(src, dest / src.name)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "could not seed %s file %s: %s", label, src.name, exc
            )
            continue
        copied += 1
    return copied


def _copy_dat_dir_if_missing(source: Path, dest: Path) -> int:
    """Seed the user-editable DAT directory on first launch (frozen builds).

    Short-circuits when ``source`` doesn't exist or already IS the destination
    — covers both dev clones (source = ``data/dats/`` at the repo root) and
    the onefile portable layout where the build script has already placed the
    DATs next to the exe.
    """
    if not source.is_dir() or source.resolve() == dest.resolve():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    has_existing = any(dest.glob("*.dat"))
    if has_existing:
        return 0
    copied = 0
    for src in sorted(source.glob("*.dat")):
        try:
            shutil.copy2(src, dest / src.name)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "could not seed DAT file %s: %s", src.name, exc
            )
            continue
        copied += 1
    return copied


def _frozen_payload_dir(subdir: str) -> Path | None:
    """Locate a bundled data subdir for first-launch seeding.

    Lookup order:

    1. ``sys._MEIPASS / subdir`` — the path where ``--onefile`` mode extracts
       embedded resources at runtime. Only present if the data subdir is
       embedded in the exe (which the current spec does NOT do for
       profiles/systems/dats — those ship as external folders next to the
       exe — but we keep the lookup so a future spec change still works).
    2. ``<install_dir>/_internal/<subdir>`` — legacy ``--onedir`` layout.
    3. ``<install_dir>/<subdir>`` — current flat layout. With profiles/
       systems/dats placed directly next to the exe by the build script,
       this resolves to the user-editable folder itself; the caller's
       source == dest short-circuit then makes seeding a no-op.

    Returns the first existing candidate, or None if no payload is found.
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / subdir)
    candidates.append(INSTALL_DIR / "_internal" / subdir)
    candidates.append(INSTALL_DIR / subdir)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def ensure_user_editable_files() -> None:
    """Create the install-dir folder layout and seed bundled defaults.

    Called once at startup, before any module looks for profiles or DATs.
    Idempotent — every step is a "create if missing" so user edits across
    launches survive.

    Layout produced at ``<install_dir>/``:

    * ``profiles/`` — destination profiles. In the portable build the ZIP
      already places the bundled YAMLs here, so seeding is a no-op (source
      == dest short-circuit).
    * ``systems/``  — system registry YAMLs. Same story as profiles.
    * ``dats/``     — No-Intro DAT files. Same story as above. In dev mode
      the repo's ``data/dats/`` is the canonical location; this dir is left
      empty unless the user manually populates it.
    * ``data/``     — SQLite DB + cover cache. Created empty.
    * ``logs/``     — rotating log file lives here.
    """
    logger = logging.getLogger(__name__)
    user_profiles = INSTALL_DIR / "profiles"
    user_systems = INSTALL_DIR / "systems"
    user_profiles.mkdir(parents=True, exist_ok=True)
    user_systems.mkdir(parents=True, exist_ok=True)

    # In a frozen build the payload sits beside the exe; copy YAMLs into
    # the user-editable location iff that location is empty. Dev runs hit
    # this branch only if someone manually deletes the in-repo files and
    # the frozen payload check above returns None, so it's a no-op there.
    frozen_profiles = _frozen_payload_dir("profiles")
    if frozen_profiles and frozen_profiles.resolve() != user_profiles.resolve():
        copied = _copy_yaml_dir_if_missing(frozen_profiles, user_profiles, "profile")
        if copied:
            logger.info("seeded %d profile file(s) into %s", copied, user_profiles)

    frozen_systems = _frozen_payload_dir("systems")
    if frozen_systems and frozen_systems.resolve() != user_systems.resolve():
        copied = _copy_yaml_dir_if_missing(frozen_systems, user_systems, "system")
        if copied:
            logger.info("seeded %d system file(s) into %s", copied, user_systems)

    if getattr(sys, "frozen", False):
        # DATs only seeded for frozen builds — dev clones have data/dats/.
        frozen_dats = _frozen_payload_dir("dats")
        if frozen_dats is not None:
            dats_dest = INSTALL_DIR / "dats"
            copied = _copy_dat_dir_if_missing(frozen_dats, dats_dest)
            if copied:
                logger.info("seeded %d DAT file(s) into %s", copied, dats_dest)

    # data/ + logs/ — created here so subsequent module imports can write.
    resolve_data_dir()
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)


class LogFileLockedError(RuntimeError):
    """Raised when the log file is held open by another process.

    Surfaced by :func:`setup_logging` so the entry-point can print a
    friendly message and exit instead of letting Python's logging
    module spam stderr with a ``PermissionError`` traceback on every
    log rotation attempt. See :func:`_log_file_is_locked` for the
    detection mechanism.
    """


def _log_file_is_locked(path: Path) -> bool:
    """True when *path* is held open by another process (Windows only).

    On Windows, a file opened for shared append by one process can't
    be renamed by a second one — and ``RotatingFileHandler`` rotates
    by renaming. Attempting ``os.rename(path, path)`` is the standard
    probe: it's a no-op when the file is yours (or doesn't exist), and
    raises ``PermissionError`` when another process holds it.

    On POSIX systems renames of open files always succeed, so the
    rotation problem doesn't exist and this returns False — same as
    "not locked" — which is the correct behaviour.

    Missing files are treated as not-locked: ``RotatingFileHandler``
    creates the file itself on first write.
    """
    if not path.exists():
        return False
    try:
        os.rename(str(path), str(path))
    except PermissionError:
        return True
    except OSError:
        # Some other rename failure (read-only filesystem, etc.) isn't
        # a "locked by another process" condition — let the regular
        # handler-construction code path surface that specific error.
        return False
    return False


def setup_logging(
    log_path: Path | str | None = None,
    level_name: str | None = None,
) -> Path:
    """Configure root-logger handlers for the desktop app.

    Routes every ``logging.getLogger(...)`` call in the codebase to:

    1. A rotating file at ``<install_dir>/logs/romulus.log`` (5 MB × 3
       backups), and
    2. ``stderr`` so a developer running ``python -m romulus`` sees output too.

    ``level_name`` is resolved in this order:

    1. Explicit argument passed in by the caller (highest precedence).
    2. ``ROMULUS_LOG_LEVEL`` env var.
    3. ``"INFO"`` default.

    Idempotent — safe to call more than once (existing handlers are removed
    first). Returns the resolved log path so callers can surface it in error
    dialogs.

    Raises :class:`LogFileLockedError` when the log file is held open by
    another ROMulus instance. Callers should catch and exit cleanly
    rather than continue — Python's :class:`RotatingFileHandler` would
    otherwise emit a ``PermissionError`` traceback on every rotation
    attempt.
    """
    resolved = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)

    chosen = level_name or os.environ.get("ROMULUS_LOG_LEVEL") or "INFO"
    chosen = chosen.upper()
    level = getattr(logging, chosen, logging.INFO)

    # Detach + close existing root handlers FIRST so any
    # RotatingFileHandler this process previously opened on the same
    # path releases its lock. Without this the idempotent-call case
    # would trip the same-process branch of ``_log_file_is_locked`` on
    # Windows (the kernel refuses to rename a file even one of our own
    # handles still owns).
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    # Probe AFTER releasing our own handles. A True result here means
    # some *other* process still owns the file — almost always a stale
    # ROMulus instance the user forgot to close.
    if _log_file_is_locked(resolved):
        raise LogFileLockedError(
            f"log file {resolved} is held open by another process"
        )

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    file_handler = RotatingFileHandler(
        str(resolved),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    for noisy in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.INFO)

    return resolved


def set_log_level(level_name: str) -> None:
    """Adjust the root logger's level at runtime.

    Used by Settings → Diagnostics so the user can switch verbosity without
    restarting the app. Unknown level names silently fall back to INFO. The
    noisy third-party loggers remain capped at INFO regardless.
    """
    normalized = (level_name or "").strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        normalized = "INFO"
    logging.getLogger().setLevel(getattr(logging, normalized))
    for noisy in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.INFO)


def resolve_db_path() -> Path:
    """Return the resolved on-disk path for ``romulus.db``.

    Lives under :func:`resolve_data_dir`. Module-level callers that historically
    grabbed ``DEFAULT_DB_PATH`` at import time should call this each time
    instead so a ``ROMULUS_DATA_DIR`` override at startup is honored.
    """
    return resolve_data_dir() / "romulus.db"


def initialize_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the app DB, create tables, and seed systems + defaults + favorites."""
    if db_path is None:
        db_path = resolve_db_path()
    conn = get_connection(db_path)
    create_tables(conn)
    seed_systems(conn)
    seed_defaults(conn)
    q.ensure_favorites_collection(conn)
    return conn


def prompt_for_library_path(parent=None) -> str:
    """Open a folder picker so the user can choose their ROM library."""
    return QFileDialog.getExistingDirectory(
        parent,
        "Select your ROM library folder",
        str(Path.home()),
    )


def ensure_library_path(conn: sqlite3.Connection, parent=None) -> str:
    """Return the saved library_path, or prompt for one and save it.

    Empty string is returned if the user cancels the dialog.
    """
    current = get_config(conn, "library_path") or ""
    if current:
        return current
    chosen = prompt_for_library_path(parent)
    if chosen:
        set_config(conn, "library_path", chosen)
    return chosen


def _app_icon_path() -> Path:
    """Absolute path to the bundled CD-ROM disc icon.

    Lives inside the package (``src/romulus/ui/icons/cdrom.ico``) so the
    PyInstaller spec includes it via the same ``collect_data_files`` sweep
    as the QSS themes.
    """
    return Path(__file__).resolve().parent / "ui" / "icons" / "cdrom.ico"


def run() -> int:
    """Bootstrap QApplication, init the DB, and show the main window."""
    # Bootstrap with env-var or INFO so any errors before the DB is up still
    # get logged. The user's stored Settings level is applied below, BUT only
    # if the env var hasn't pinned a level — otherwise users couldn't override
    # via ``ROMULUS_LOG_LEVEL=DEBUG`` for diagnostics.
    try:
        log_path = setup_logging()
    except LogFileLockedError as exc:
        # Another ROMulus instance has the log file open. Refuse to start
        # rather than push through with a broken rotating handler that
        # would dump a PermissionError traceback on every rotation.
        # QApplication isn't constructed yet so we can't show a Qt dialog;
        # a clear stderr message + non-zero exit is the next best thing.
        print(
            f"ROMulus: cannot start — {exc}.\n"
            "Close any other running copy of ROMulus and try again.",
            file=sys.stderr,
        )
        return 1
    logger = logging.getLogger("romulus")
    logger.info("ROMulus starting up (log file: %s)", log_path)
    ensure_user_editable_files()
    app = QApplication.instance() or QApplication(sys.argv)
    # Apply the app icon BEFORE any window is constructed so MainWindow,
    # dialogs, and the taskbar entry all inherit it.
    icon_path = _app_icon_path()
    if icon_path.is_file():
        from PySide6.QtGui import QIcon

        app.setWindowIcon(QIcon(str(icon_path)))
    conn = initialize_database(resolve_db_path())
    # Resolve final log level: env var wins if set, otherwise SQLite config,
    # otherwise the INFO default already applied by setup_logging.
    env_level = os.environ.get("ROMULUS_LOG_LEVEL")
    if env_level:
        logger.info("log level pinned by ROMULUS_LOG_LEVEL=%s", env_level)
    else:
        configured = get_config(conn, "log_level") or "INFO"
        set_log_level(configured)
    # Late import is INTENTIONAL: ``MainWindow`` (and the chain of Qt widget
    # classes it pulls in) requires a live QApplication to exist before any
    # QWidget subclass is even imported on some Qt builds. Moving this back to
    # the top of the file regresses headless startup. Do not "clean up".
    from romulus.ui.main_window import MainWindow
    from romulus.ui.themes import apply_theme

    theme = get_config(conn, "theme") or "system"
    apply_theme(app, theme)

    window = MainWindow(conn)
    ensure_library_path(conn, window)
    window.refresh_all()
    window.show()
    return app.exec()
