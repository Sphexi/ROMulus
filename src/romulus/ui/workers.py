"""QThread workers for background operations (scan, hash, enrich, export).

Each worker opens its own sqlite3 connection on the worker thread — sqlite3
connections are thread-bound by default, so reusing the main-thread connection
from a QThread is unsafe. Workers communicate back via Qt signals which are
queued onto the main thread by Qt's event loop.

The four concrete workers (Scan, Enrich, Organize, Export) share a single
:class:`_DbWorker` base class that owns:

* the thread-local DB connection lifecycle (open in ``run``, close on every exit),
* the cooperative-cancel pattern (``cancel()`` flips a flag that the progress
  callback checks; raising :class:`_WorkerCancelled` from the callback unwinds
  the work cleanly),
* the ``failed`` signal plumbing for unexpected exceptions.

Each concrete worker only has to override :meth:`_DbWorker._run_work` with the
actual work and emit its own ``finished_ok`` signal on success.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from romulus.core import scan_library
from romulus.core.exporter import (
    ExportFilters,
    ExportOptions,
    ExportSummary,
    export_collection,
)
from romulus.core.organizer import OrganizeAction, OrganizeSummary, execute_plan
from romulus.db import get_connection
from romulus.metadata import enrich_library
from romulus.models.profile import DestinationProfile

logger = logging.getLogger(__name__)


class _WorkerCancelled(Exception):  # noqa: N818 - cancel marker, not an error
    """Shared cooperative-cancel marker raised from a worker's progress callback.

    Replaces the four near-identical private exception classes the workers used
    to declare individually. The exception is internal — callers see a
    ``failed`` signal with a human-readable cancellation message. We omit the
    ``Error`` suffix on purpose: this is a control-flow signal, not a failure.
    """


class _DbWorker(QThread):
    """Base class for every QThread worker that needs a thread-local DB connection.

    Subclasses override :meth:`_run_work` and emit their own ``finished_ok``
    signal on success. Cancellation flows through the shared ``cancel()`` flag
    + a :class:`_WorkerCancelled` raised from the progress callback. Unexpected
    exceptions surface via the shared :pyattr:`failed` signal as
    ``"{operation} failed ({ExceptionType})"`` — exception text is logged for
    forensics but never echoed to the UI.
    """

    #: Emitted on any unrecoverable error (DB open failure, work exception, or
    #: cooperative cancel). Carries a single human-readable string of the form
    #: ``"{Operation} failed ({ExceptionType})"`` or
    #: ``"{Operation} cancelled"``. The raw exception text is never included
    #: (security audit v0.1.0 finding #12) — it is logged via :mod:`logging`
    #: for forensics instead.
    failed = Signal(str)

    #: Subclasses override this to customise the cancel-message prefix
    #: (e.g. ``"Scan"`` -> ``"Scan cancelled"``).
    _operation_name: str = "Operation"

    def __init__(self, db_path: Path | str) -> None:
        super().__init__()
        self._db_path = db_path
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cooperative cancellation; checked on every progress tick."""
        self._cancel_requested = True

    def _check_cancel(self) -> None:
        """Raise :class:`_WorkerCancelled` if cancellation has been requested.

        Subclasses call this from inside their progress callbacks so the
        executing work function unwinds at the next reported tick.
        """
        if self._cancel_requested:
            raise _WorkerCancelled

    def run(self) -> None:  # noqa: D401 - QThread API
        """Open a thread-local DB connection, run the work, emit signals.

        Exception sanitization (security audit v0.1.0 finding #12): the full
        traceback (including any path/credential the exception text might
        carry) is logged via :mod:`logging` for forensics. The user-facing
        ``failed`` signal carries only the exception type name plus a short
        operation prefix, never ``str(exc)`` — so a future code path that
        raises an exception containing PII or a secret can't end up in a
        ``QMessageBox`` verbatim.
        """
        try:
            conn = get_connection(self._db_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker failed to open database")
            self.failed.emit(
                f"Failed to open database ({type(exc).__name__})"
            )
            return

        try:
            self._run_work(conn)
        except _WorkerCancelled:
            self.failed.emit(f"{self._operation_name} cancelled")
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s worker failed", self._operation_name)
            self.failed.emit(
                f"{self._operation_name} failed ({type(exc).__name__})"
            )
        finally:
            conn.close()

    def _run_work(self, conn: sqlite3.Connection) -> None:
        """Subclasses do their work here and emit ``finished_ok`` on success.

        Raise :class:`_WorkerCancelled` (typically by calling
        :meth:`_check_cancel` from a progress callback) for cooperative cancel.
        Any other exception propagates out and is turned into a ``failed``
        signal by :meth:`run`.
        """
        raise NotImplementedError


class ScanWorker(_DbWorker):
    """Run `scan_library` against a library path on a worker thread.

    Note: ``progress`` is ``(count, filename)`` — unlike the other three
    workers (which all use ``(current, total, label)``). This divergence is
    intentional: the scanner discovers the file list as it walks, so it has
    no ``total`` to report up front. :class:`ScanProgressDialog` accommodates
    this by running in indeterminate mode for scan and determinate mode for
    the others. Standardizing on ``(current, total | None, label)`` is
    tracked as a v0.3.0 follow-up.
    """

    progress = Signal(int, str)
    finished_ok = Signal(int, int, int, int, list)

    _operation_name = "Scan"

    def __init__(self, db_path: Path | str, library_path: Path | str) -> None:
        super().__init__(db_path)
        self._library_path = str(library_path)

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(count: int, filename: str) -> None:
            self._check_cancel()
            self.progress.emit(count, filename)

        result = scan_library(conn, self._library_path, _progress)
        self.finished_ok.emit(
            result.scan_id,
            result.files_found,
            result.files_with_system,
            result.files_skipped,
            sorted(result.systems_seen),
        )


class EnrichWorker(_DbWorker):
    """Run `enrich_library` against the configured DB on a worker thread."""

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int)

    _operation_name = "Enrichment"

    def __init__(
        self,
        db_path: Path | str,
        cache_dir: Path | str | None = None,
        launchbox_xml_path: Path | str | None = None,
    ) -> None:
        super().__init__(db_path)
        self._cache_dir = cache_dir
        self._launchbox_xml_path = launchbox_xml_path

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(idx: int, total: int, title: str) -> None:
            self._check_cancel()
            self.progress.emit(idx, total, title)

        stats = enrich_library(
            conn,
            cache_dir=self._cache_dir,
            progress_callback=_progress,
            launchbox_xml_path=self._launchbox_xml_path,
        )
        self.finished_ok.emit(
            stats["games_processed"],
            stats["metadata_added"],
            stats["covers_added"],
        )


class OrganizeWorker(_DbWorker):
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

    _operation_name = "Organize"

    def __init__(
        self,
        db_path: Path | str,
        actions: list[OrganizeAction],
    ) -> None:
        super().__init__(db_path)
        self._actions = list(actions)

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, source: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, source)

        summary: OrganizeSummary = execute_plan(conn, self._actions, _progress)
        self.finished_ok.emit(
            summary.applied,
            summary.skipped,
            summary.failed,
            list(summary.errors),
        )


class ExportWorker(_DbWorker):
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

    _operation_name = "Export"

    def __init__(
        self,
        db_path: Path | str,
        profile: DestinationProfile,
        target_path: Path | str,
        filters: ExportFilters | None = None,
        options: ExportOptions | None = None,
    ) -> None:
        super().__init__(db_path)
        self._profile = profile
        self._target_path = str(target_path)
        self._filters = filters
        self._options = options

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, filename: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, filename)

        summary: ExportSummary = export_collection(
            conn,
            self._profile,
            self._target_path,
            filters=self._filters,
            options=self._options,
            progress_callback=_progress,
        )
        self.finished_ok.emit(
            int(summary.files_copied),
            int(summary.files_skipped),
            int(summary.bytes_copied),
            list(summary.systems),
            list(summary.errors),
        )
