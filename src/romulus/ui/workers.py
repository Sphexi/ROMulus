"""QThread workers for background operations (scan, hash, enrich, export).

Each worker opens its own sqlite3 connection on the worker thread — sqlite3
connections are thread-bound by default, so reusing the main-thread connection
from a QThread is unsafe. Workers communicate back via Qt signals which are
queued onto the main thread by Qt's event loop.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from romulus.core import scan_library
from romulus.db import get_connection


class ScanWorker(QThread):
    """Run `scan_library` against a library path on a worker thread."""

    progress = Signal(int, str)
    finished_ok = Signal(int, int, int, int, list)
    failed = Signal(str)

    def __init__(self, db_path: Path | str, library_path: Path | str) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_path = str(library_path)
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cooperative cancellation; checked on every progress tick."""
        self._cancel_requested = True

    def run(self) -> None:  # noqa: D401 - QThread API
        """Open a thread-local DB connection, scan, emit results."""
        try:
            conn = get_connection(self._db_path)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Failed to open database: {exc}")
            return

        def _progress(count: int, filename: str) -> None:
            if self._cancel_requested:
                raise _ScanCancelledError
            self.progress.emit(count, filename)

        try:
            result = scan_library(conn, self._library_path, _progress)
        except _ScanCancelledError:
            conn.close()
            self.failed.emit("Scan cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            conn.close()
            self.failed.emit(f"Scan failed: {exc}")
            return

        conn.close()
        self.finished_ok.emit(
            result.scan_id,
            result.files_found,
            result.files_with_system,
            result.files_skipped,
            sorted(result.systems_seen),
        )


class _ScanCancelledError(Exception):
    """Internal marker exception raised from the progress callback on cancel."""
