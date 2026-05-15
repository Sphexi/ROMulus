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
from romulus.core.exporter import (
    ExportFilters,
    ExportOptions,
    ExportSummary,
    export_collection,
)
from romulus.core.organizer import OrganizeAction, execute_plan
from romulus.db import get_connection
from romulus.metadata import enrich_library
from romulus.models.profile import DestinationProfile


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


class EnrichWorker(QThread):
    """Run `enrich_library` against the configured DB on a worker thread."""

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int)
    failed = Signal(str)

    def __init__(
        self,
        db_path: Path | str,
        cache_dir: Path | str | None = None,
        launchbox_xml_path: Path | str | None = None,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._cache_dir = cache_dir
        self._launchbox_xml_path = launchbox_xml_path
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cooperative cancellation; checked on every progress tick."""
        self._cancel_requested = True

    def run(self) -> None:  # noqa: D401 - QThread API
        """Open a thread-local DB connection, run enrichment, emit results."""
        try:
            conn = get_connection(self._db_path)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Failed to open database: {exc}")
            return

        def _progress(idx: int, total: int, title: str) -> None:
            if self._cancel_requested:
                raise _EnrichCancelledError
            self.progress.emit(idx, total, title)

        try:
            stats = enrich_library(
                conn,
                cache_dir=self._cache_dir,
                progress_callback=_progress,
                launchbox_xml_path=self._launchbox_xml_path,
            )
        except _EnrichCancelledError:
            conn.close()
            self.failed.emit("Enrichment cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            conn.close()
            self.failed.emit(f"Enrichment failed: {exc}")
            return

        conn.close()
        self.finished_ok.emit(
            stats["games_processed"],
            stats["metadata_added"],
            stats["covers_added"],
        )


class _EnrichCancelledError(Exception):
    """Internal marker exception raised from enrich progress on cancel."""


class OrganizeWorker(QThread):
    """Apply an approved set of :class:`OrganizeAction` items on a worker thread.

    Mirrors the ScanWorker / EnrichWorker contract: opens a thread-local
    sqlite3 connection inside ``run``, emits ``progress(current, total,
    source_path)`` per action, ``finished_ok(applied, skipped, failed,
    errors)`` on success, ``failed(msg)`` on exception. Cooperative cancel
    works the same way as the other workers — a private exception raised from
    the progress callback unwinds the executor.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int, list)
    failed = Signal(str)

    def __init__(
        self,
        db_path: Path | str,
        actions: list[OrganizeAction],
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._actions = list(actions)
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cooperative cancellation; checked on every progress tick."""
        self._cancel_requested = True

    def run(self) -> None:  # noqa: D401 - QThread API
        """Open a thread-local DB connection, execute the plan, emit results."""
        try:
            conn = get_connection(self._db_path)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Failed to open database: {exc}")
            return

        def _progress(current: int, total: int, source: str) -> None:
            if self._cancel_requested:
                raise _OrganizeCancelledError
            self.progress.emit(current, total, source)

        try:
            summary = execute_plan(conn, self._actions, _progress)
        except _OrganizeCancelledError:
            conn.close()
            self.failed.emit("Organize cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            conn.close()
            self.failed.emit(f"Organize failed: {exc}")
            return

        conn.close()
        self.finished_ok.emit(
            int(summary.get("applied", 0)),
            int(summary.get("skipped", 0)),
            int(summary.get("failed", 0)),
            list(summary.get("errors", [])),
        )


class _OrganizeCancelledError(Exception):
    """Internal marker exception raised from organize progress on cancel."""


class ExportWorker(QThread):
    """Run :func:`export_collection` on a worker thread.

    Mirrors the ScanWorker / EnrichWorker / OrganizeWorker contract: opens a
    thread-local sqlite3 connection inside ``run``, emits ``progress(current,
    total, filename)`` per ROM, ``finished_ok(files_copied, files_skipped,
    bytes_copied, systems, errors)`` on success, ``failed(msg)`` on
    exception. Cooperative cancel works the same way as the other workers —
    a private exception raised from the progress callback unwinds the export.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int, list, list)
    failed = Signal(str)

    def __init__(
        self,
        db_path: Path | str,
        profile: DestinationProfile,
        target_path: Path | str,
        filters: ExportFilters | None = None,
        options: ExportOptions | None = None,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._profile = profile
        self._target_path = str(target_path)
        self._filters = filters
        self._options = options
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cooperative cancellation; checked on every progress tick."""
        self._cancel_requested = True

    def run(self) -> None:  # noqa: D401 - QThread API
        """Open a thread-local DB connection, run the export, emit results."""
        try:
            conn = get_connection(self._db_path)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Failed to open database: {exc}")
            return

        def _progress(current: int, total: int, filename: str) -> None:
            if self._cancel_requested:
                raise _ExportCancelledError
            self.progress.emit(current, total, filename)

        try:
            summary: ExportSummary = export_collection(
                conn,
                self._profile,
                self._target_path,
                filters=self._filters,
                options=self._options,
                progress_callback=_progress,
            )
        except _ExportCancelledError:
            conn.close()
            self.failed.emit("Export cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            conn.close()
            self.failed.emit(f"Export failed: {exc}")
            return

        conn.close()
        self.finished_ok.emit(
            int(summary.files_copied),
            int(summary.files_skipped),
            int(summary.bytes_copied),
            list(summary.systems),
            list(summary.errors),
        )


class _ExportCancelledError(Exception):
    """Internal marker exception raised from export progress on cancel."""
