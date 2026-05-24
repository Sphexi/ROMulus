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
from romulus.core.dat_parser import load_all_dats, match_hashes
from romulus.core.dest_inventory import DestInventory, scan_destination
from romulus.core.exporter import (
    ExportFilters,
    ExportOptions,
    ExportSummary,
    export_collection,
)
from romulus.core.hasher import hash_library
from romulus.core.importer import (
    ImportOptions,
    ImportPlan,
    ImportSummary,
)
from romulus.core.importer import analyse_import as _analyse_import
from romulus.core.importer import apply_plan as _apply_import_plan
from romulus.core.local_cover_finder import DiscoveryResult, discover_local_covers
from romulus.core.organizer import OrganizeAction, OrganizeSummary, execute_plan
from romulus.core.scrub import ScrubAction, ScrubPlan, ScrubSummary
from romulus.core.scrub import analyse as _scrub_analyse
from romulus.core.scrub import apply_plan as _scrub_apply_plan
from romulus.core.sync import (
    ConflictPolicy,
    SyncAction,
    SyncMode,
    SyncPlan,
    SyncSummary,
    apply_plan,
    build_plan,
    persist_plan,
)
from romulus.db import get_connection
from romulus.db import queries as q
from romulus.metadata import enrich_library, fetch_online_covers_for_scope
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

    During the post-walk DB phases (missing sweep, scan history finalisation)
    the scanner emits progress events with phase labels — so the user sees
    "Marking missing entries…" / "Finalising scan history…" instead of a
    frozen Cancel button. The dialog detects these labels (any string ending
    with a literal Unicode ellipsis) and disables the Cancel button itself —
    a mid-rebuild cancel would leave the DB partial, so the worker also stops
    honouring ``_check_cancel`` once it sees a post-walk label.

    ``scope_system_id`` restricts the scan to one platform — see
    :func:`romulus.core.scanner.scan_library`. Wired to the sidebar
    right-click "Quick Scan <system>" action.
    """

    progress = Signal(int, str)
    finished_ok = Signal(int, int, int, int, list)

    _operation_name = "Scan"

    def __init__(
        self,
        db_path: Path | str,
        library_path: Path | str,
        *,
        scope_system_id: str | None = None,
    ) -> None:
        super().__init__(db_path)
        self._library_path = str(library_path)
        self._scope_system_id = scope_system_id
        self._post_walk = False

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(count: int, filename: str) -> None:
            # Post-walk phases emit labels ending with the literal
            # Unicode ellipsis (see scanner.py). The first such label
            # flips the worker into post-walk mode where cancel is
            # ignored (the post-walk DB work has no safe abort points;
            # a half-done sweep would leave the DB inconsistent with
            # disk). The receiving dialog detects the same marker and
            # disables the Cancel button visually.
            if not self._post_walk and filename.endswith("…"):
                self._post_walk = True
            if not self._post_walk:
                self._check_cancel()
            self.progress.emit(count, filename)

        result = scan_library(
            conn,
            self._library_path,
            _progress,
            scope_system_id=self._scope_system_id,
        )
        self.finished_ok.emit(
            result.scan_id,
            result.files_found,
            result.files_with_system,
            result.files_skipped,
            sorted(result.systems_seen),
        )


class EnrichWorker(_DbWorker):
    """Run `enrich_library` against the configured DB on a worker thread.

    Optional scope kwargs narrow processing to a specific set of ROMs:
        rom_ids: Limit to these ROM ids.
        system_id: Limit to ROMs in this system.
        collection_id: Limit to ROMs in this collection.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int)

    _operation_name = "Enrichment"

    def __init__(
        self,
        db_path: Path | str,
        cache_dir: Path | str | None = None,
        launchbox_xml_path: Path | str | None = None,
        *,
        rom_ids: list[int] | None = None,
        system_id: str | None = None,
        collection_id: int | None = None,
        include_fuzzy: bool = False,
        include_already_enriched: bool = False,
        include_online: bool = True,
    ) -> None:
        super().__init__(db_path)
        self._cache_dir = cache_dir
        self._launchbox_xml_path = launchbox_xml_path
        self._rom_ids = rom_ids
        self._system_id = system_id
        self._collection_id = collection_id
        self._include_fuzzy = include_fuzzy
        self._include_already_enriched = include_already_enriched
        self._include_online = include_online

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(idx: int, total: int, title: str) -> None:
            self._check_cancel()
            self.progress.emit(idx, total, title)

        stats = enrich_library(
            conn,
            cache_dir=self._cache_dir,
            progress_callback=_progress,
            launchbox_xml_path=self._launchbox_xml_path,
            rom_ids=self._rom_ids,
            system_id=self._system_id,
            collection_id=self._collection_id,
            include_fuzzy=self._include_fuzzy,
            include_already_enriched=self._include_already_enriched,
            include_online=self._include_online,
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


class HeavyScanWorker(_DbWorker):
    """Hash every ROM, load bundled DATs if needed, and run DAT matching.

    Emits ``progress(hashed, total, filename)`` per completed file.
    Emits ``finished_ok(total_hashed, total_matched, errors)`` on success.
    Emits ``failed(msg)`` on exception or cancel.

    On first use (empty ``dat_entries`` table) the worker loads every folder
    in ``dat_paths`` before hashing — this adds ~6 s but subsequent runs skip
    it. The caller passes ``config.dat_paths`` straight through so the
    portable-build install dir AND any user-added folders both get loaded.

    Optional ``scope_rom_ids`` limits both hashing and DAT matching to a
    subset of ROMs so a scoped Heavy Scan never touches state outside its
    scope.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int)

    _operation_name = "Heavy Scan"

    def __init__(
        self,
        db_path: Path | str,
        library_path: Path | str,
        dat_paths: list[Path | str],
        workers: int = 8,
        *,
        scope_rom_ids: list[int] | None = None,
    ) -> None:
        super().__init__(db_path)
        self._library_path = str(library_path)
        self._dat_paths = [Path(p) for p in dat_paths]
        self._workers = workers
        self._scope_rom_ids = scope_rom_ids

    def _run_work(self, conn: sqlite3.Connection) -> None:
        scope_kind = (
            "scoped" if self._scope_rom_ids is not None else "library-wide"
        )
        logger.info(
            "HeavyScan start: %s scope (rom_ids=%s) workers=%d dat_paths=%s",
            scope_kind,
            (
                None
                if self._scope_rom_ids is None
                else f"{len(self._scope_rom_ids)} ids"
            ),
            self._workers,
            [str(p) for p in self._dat_paths],
        )

        # Load DATs on first run (empty dat_entries table).
        dat_count = conn.execute(
            "SELECT COUNT(*) FROM dat_entries"
        ).fetchone()[0]
        if dat_count == 0:
            logger.info(
                "HeavyScan: loading DATs (empty dat_entries table)"
            )
            self.progress.emit(0, 0, "Loading DATs…")
            inserted = load_all_dats(conn, self._dat_paths)
            logger.info("HeavyScan: loaded %d DAT entries", inserted)
            if inserted == 0:
                logger.warning(
                    "HeavyScan: no DAT entries loaded — check that "
                    "dat_paths point to folders containing .dat/.xml "
                    "files (configured paths: %s)",
                    [str(p) for p in self._dat_paths],
                )
        else:
            logger.info(
                "HeavyScan: dat_entries already populated (%d rows) — skipping DAT load",
                dat_count,
            )

        errors = 0

        def _progress(done: int, total: int, path: str) -> None:
            self._check_cancel()
            self.progress.emit(done, total, path)

        # Surface "scanning..." even when there's no work to do so the
        # dialog isn't blank during the rapid scan -> finished_ok flip.
        self.progress.emit(0, 0, "Checking for ROMs needing hashing…")
        total_hashed = hash_library(
            conn,
            progress_callback=_progress,
            workers=self._workers,
            scope_rom_ids=self._scope_rom_ids,
        )
        logger.info(
            "HeavyScan: hashed %d ROM(s) (rest were already cached)",
            total_hashed,
        )

        self.progress.emit(0, 0, "Matching against DAT database…")
        total_matched = match_hashes(conn, scope_rom_ids=self._scope_rom_ids)
        logger.info(
            "HeavyScan complete: hashed=%d matched=%d errors=%d",
            total_hashed,
            total_matched,
            errors,
        )
        self.finished_ok.emit(total_hashed, total_matched, errors)


class CoverFinderWorker(_DbWorker):
    """Run local cover discovery and/or online cover fetching on a worker thread.

    Signals:
        progress(current, total, filename): Emitted once per ROM/game processed.
        finished_ok(roms_scanned, covers_found, covers_skipped, errors, online_covers):
            Final counts. ``online_covers`` is the count of libretro
            thumbnails inserted during the online phase (0 when that
            phase was skipped).
        failed(message): Emitted on exception or cooperative cancel.

    Two independent toggles:

    * ``include_local`` (default True) — walk the configured library
      and link image files found alongside ROMs.
    * ``include_online`` (default False) — fetch libretro thumbnails
      for every game in scope that doesn't already have one.

    Both phases share the same ``scope_rom_ids`` filter so a system or
    collection scope narrows both. When both flags are False nothing
    runs and the worker emits zeros — callers should validate that at
    least one mode is enabled before constructing the worker.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int, int, int)

    _operation_name = "Cover Discovery"

    def __init__(
        self,
        db_path: Path | str,
        library_path: Path | str,
        *,
        scope_rom_ids: list[int] | None = None,
        include_local: bool = True,
        include_online: bool = False,
        cache_dir: Path | str | None = None,
    ) -> None:
        super().__init__(db_path)
        self._library_path = str(library_path)
        self._scope_rom_ids = scope_rom_ids
        self._include_local = include_local
        self._include_online = include_online
        self._cache_dir = cache_dir

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, filename: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, filename)

        # Phase 1: local image-file discovery. Populates the same
        # roms_scanned / covers_found / covers_skipped / errors counts
        # the dialog used to receive — preserves on-screen wording.
        if self._include_local:
            result: DiscoveryResult = discover_local_covers(
                conn,
                self._library_path,
                progress_callback=_progress,
                scope_rom_ids=self._scope_rom_ids,
            )
            roms_scanned = result.roms_scanned
            covers_found = result.covers_found
            covers_skipped = result.covers_skipped_existing
            errors = result.errors
        else:
            roms_scanned = 0
            covers_found = 0
            covers_skipped = 0
            errors = 0

        # Phase 2: online libretro-thumbnail fetch for the same scope.
        # Operates per-rom — the sibling-copy gate inside the helper
        # avoids redundant network calls for byte-identical duplicates.
        online_covers = 0
        if self._include_online:
            online_covers = fetch_online_covers_for_scope(
                conn,
                scope_rom_ids=self._scope_rom_ids,
                cache_dir=self._cache_dir,
                progress_callback=_progress,
            )

        self.finished_ok.emit(
            roms_scanned,
            covers_found,
            covers_skipped,
            errors,
            online_covers,
        )


# Backwards-compatible alias for the rename above. Anything that
# imported ``LocalCoverFinderWorker`` keeps working; new code should
# use :class:`CoverFinderWorker`.
LocalCoverFinderWorker = CoverFinderWorker


class DestInventoryWorker(_DbWorker):
    """Walk a destination, refresh ``dest_inventory``, return a :class:`DestInventory`.

    Mirrors the existing worker contract: thread-local sqlite3 connection,
    ``progress(current, total, label)`` ticks, ``finished_ok(...)`` on
    success, ``failed(msg)`` on exception or cancel. The signature drift
    detection in :func:`romulus.core.dest_inventory.scan_destination` runs
    automatically — the worker doesn't need to wire it explicitly.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(object)

    _operation_name = "Destination scan"

    def __init__(
        self,
        db_path: Path | str,
        dest_id: int,
        target_path: Path | str,
        *,
        deep_verify: bool = False,
    ) -> None:
        super().__init__(db_path)
        self._dest_id = dest_id
        self._target_path = str(target_path)
        self._deep_verify = deep_verify

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, label: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, label)

        inventory: DestInventory = scan_destination(
            conn,
            self._dest_id,
            self._target_path,
            deep_verify=self._deep_verify,
            progress_callback=_progress,
        )
        self.finished_ok.emit(inventory)


class BuildSyncPlanWorker(_DbWorker):
    """Run :func:`romulus.core.sync.build_plan` on a worker thread.

    Sits between :class:`DestInventoryWorker` and the
    :class:`SyncPreviewDialog`. ``build_plan`` was running on the UI
    thread inside the inventory-finished slot — on a large library
    (38 K local × 17 K dest) that produced a multi-second "not
    responding" window because the slot fires on the receiving
    (UI) thread regardless of where the signal originated. The new
    worker gives the diff phase the same QThread + cooperative-cancel
    contract every other long-running op already uses.
    """

    progress = Signal(int, int, str)
    #: Emits the populated :class:`SyncPlan` once the diff finishes.
    finished_ok = Signal(object)

    _operation_name = "Sync diff"

    def __init__(
        self,
        db_path: Path | str,
        dest_id: int,
        profile: DestinationProfile,
        target_path: Path | str,
        inventory: SyncPlan | object,
        mode: SyncMode,
        *,
        conflict_policy: ConflictPolicy = "skip",
        library_path: Path | str | None = None,
    ) -> None:
        super().__init__(db_path)
        self._dest_id = dest_id
        self._profile = profile
        self._target_path = str(target_path)
        self._inventory = inventory
        self._mode = mode
        self._conflict_policy = conflict_policy
        self._library_path = (
            str(library_path) if library_path is not None else None
        )

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, label: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, label)

        # Mypy can't narrow the ``object`` parameter shape; the caller
        # passes a real DestInventory in practice but the wider typing
        # avoids dragging the dest_inventory module into every consumer.
        plan: SyncPlan = build_plan(
            conn,
            self._dest_id,
            self._profile,
            self._target_path,
            self._inventory,  # type: ignore[arg-type]
            self._mode,
            conflict_policy=self._conflict_policy,
            library_path=self._library_path,
            progress_callback=_progress,
        )
        self.finished_ok.emit(plan)


class SyncWorker(_DbWorker):
    """Build a plan, persist it, apply it, emit per-action progress.

    The worker takes the same parameters the diff engine needs (mode,
    profile, target, inventory) plus the user-approved action list — the
    preview dialog filters out unchecked actions before signalling.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int, int, list)
    #: Emits the full :class:`SyncSummary` object so the per-system
    #: summary dialog can render the breakdown. Fires AFTER ``finished_ok``.
    summary_ready = Signal(object)

    _operation_name = "Sync"

    def __init__(
        self,
        db_path: Path | str,
        dest_id: int,
        profile: DestinationProfile,
        target_path: Path | str,
        plan: SyncPlan,
        approved_actions: list[SyncAction],
        *,
        library_path: Path | str | None = None,
    ) -> None:
        super().__init__(db_path)
        self._dest_id = dest_id
        self._profile = profile
        self._target_path = str(target_path)
        self._plan = plan
        self._approved_actions = list(approved_actions)
        self._library_path = (
            str(library_path) if library_path is not None else None
        )

    def _run_work(self, conn: sqlite3.Connection) -> None:
        # Persist the plan up-front so a crash mid-sync leaves an audit trail.
        # Build a plan-shaped copy that contains only the approved actions —
        # the preview dropped anything the user unchecked.
        filtered = SyncPlan(
            dest_id=self._plan.dest_id,
            mode=self._plan.mode,
            actions=list(self._approved_actions),
            conflict_policy=self._plan.conflict_policy,
        )
        persist_plan(conn, filtered, status="pending")

        def _progress(current: int, total: int, label: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, label)

        summary: SyncSummary = apply_plan(
            conn,
            filtered,
            self._profile,
            self._target_path,
            library_path=self._library_path,
            progress_callback=_progress,
        )
        self.finished_ok.emit(
            summary.applied,
            summary.skipped,
            summary.failed,
            list(summary.errors),
        )
        self.summary_ready.emit(summary)


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
    # ``bytes_copied`` declared as qint64 (Qt's signed 64-bit) so totals
    # past 2 GiB don't wrap to negative in the summary. Python ints are
    # unbounded but PySide6 marshals plain ``int`` through a C ``int``
    # which is 32-bit signed; a 11 GB export reported as -783 MB.
    finished_ok = Signal(int, int, "qint64", list, list)
    #: Emits the full :class:`ExportSummary` object so the per-system
    #: summary dialog can render the breakdown. Fires AFTER ``finished_ok``.
    summary_ready = Signal(object)

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
        self.summary_ready.emit(summary)


class ImportAnalyseWorker(_DbWorker):
    """Walk a staging folder and emit a populated :class:`ImportPlan`.

    Mirrors :class:`DestInventoryWorker` — both produce a "what would
    happen" snapshot used by a follow-up preview dialog. The apply step
    is a separate worker (:class:`ImportApplyWorker`) so each phase
    surfaces its own progress + failure path. Keeping them split also
    means a Cancel during analyse doesn't leave a half-applied plan on
    disk.
    """

    progress = Signal(int, int, str)
    # The plan is a Python object; emit it as ``object`` so the receiving
    # main_window slot can isinstance-check before using it. Qt's signal
    # system doesn't carry user-defined dataclasses natively, so the
    # generic ``object`` slot is what every other worker that emits a
    # complex result uses (see ``DestInventoryWorker.finished_ok``).
    finished_ok = Signal(object)

    _operation_name = "Import analyse"

    def __init__(
        self,
        db_path: Path | str,
        staging_path: Path | str,
        library_path: Path | str,
        options: ImportOptions,
    ) -> None:
        super().__init__(db_path)
        self._staging_path = str(staging_path)
        self._library_path = str(library_path)
        self._options = options

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, filename: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, filename)

        plan: ImportPlan = _analyse_import(
            conn,
            self._staging_path,
            self._library_path,
            options=self._options,
            progress_callback=_progress,
        )
        self.finished_ok.emit(plan)


class ImportApplyWorker(_DbWorker):
    """Apply an approved :class:`ImportPlan` against the local library.

    Per-action SAVEPOINT rollback, atomic copy via :mod:`romulus.core.atomic`,
    cooperative cancel between actions. Mirrors :class:`SyncWorker`'s shape
    one-for-one — both run a plan-of-actions through a single ``apply_plan``
    function and emit per-action progress.
    """

    progress = Signal(int, int, str)
    # ``bytes_imported`` declared as qint64 so totals past 2 GiB don't wrap
    # negative — same precaution :class:`ExportWorker` already takes for its
    # bytes-copied counter.
    finished_ok = Signal(int, int, int, int, "qint64", list, list)

    _operation_name = "Import"

    def __init__(
        self,
        db_path: Path | str,
        plan: ImportPlan,
    ) -> None:
        super().__init__(db_path)
        self._plan = plan

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, filename: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, filename)

        summary: ImportSummary = _apply_import_plan(
            conn,
            self._plan,
            progress_callback=_progress,
        )
        self.finished_ok.emit(
            int(summary.files_imported),
            int(summary.files_skipped),
            int(summary.files_replaced),
            int(summary.files_kept_both),
            int(summary.bytes_imported),
            sorted(summary.systems_touched),
            list(summary.errors),
        )


class ScrubAnalyseWorker(_DbWorker):
    """Walk the DB and classify rows against disk; emit a populated ScrubPlan.

    Mirrors :class:`ImportAnalyseWorker` — produces a "what would happen"
    snapshot consumed by a follow-up preview dialog. Apply is a separate
    worker (:class:`ScrubApplyWorker`) so each phase surfaces its own
    progress + failure path. Read-only — never writes to the DB; safe to
    cancel mid-walk without leaving anything inconsistent.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(object)

    _operation_name = "Verify Library"

    def __init__(
        self,
        db_path: Path | str,
        library_root: Path | str,
    ) -> None:
        super().__init__(db_path)
        self._library_root = str(library_root)

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, filename: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, filename)

        plan: ScrubPlan = _scrub_analyse(
            conn,
            self._library_root,
            progress_callback=_progress,
        )
        self.finished_ok.emit(plan)


class ScrubApplyWorker(_DbWorker):
    """Apply an approved set of :class:`ScrubAction` items, bucketed by status.

    Per-bucket SAVEPOINT — each of the four buckets commits independently.
    A failure in one bucket rolls back only that bucket, the others apply.
    Cancellation is cooperative between actions (mid-bucket cancel rolls
    back the in-flight bucket).
    """

    progress = Signal(int, int, str)
    # (flagged_missing, deleted_outside_root, untombstoned, drift_fixed,
    #  pruned_games, errors)
    finished_ok = Signal(int, int, int, int, int, list)

    _operation_name = "Verify Library apply"

    def __init__(
        self,
        db_path: Path | str,
        actions: list[ScrubAction],
    ) -> None:
        super().__init__(db_path)
        self._actions = list(actions)

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, label: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, label)

        try:
            summary: ScrubSummary = _scrub_apply_plan(
                conn,
                self._actions,
                progress_callback=_progress,
            )
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.exception("ScrubApply: rollback after failure errored")
            raise

        self.finished_ok.emit(
            summary.flagged_missing,
            summary.deleted_outside_root,
            summary.untombstoned,
            summary.drift_fixed,
            summary.pruned_games,
            list(summary.errors),
        )


class CleanMissingWorker(_DbWorker):
    """Permanently delete every ``missing = 1`` row + prune orphan games.

    Mirrors the other long-running workers' contract: thread-local sqlite3
    connection, ``progress(current, total, label)`` ticks per chunk,
    ``finished_ok(deleted_roms, pruned_games)`` on success, ``failed(msg)``
    on exception or cancel.

    The work runs as a single transaction on the worker's own connection:
    ``delete_missing_roms`` -> ``commit()``. Any exception triggers
    ``conn.rollback()`` BEFORE the exception unwinds out of
    :meth:`_run_work`, so :meth:`_DbWorker.run` sees a clean connection
    when it logs the failure. The rollback also unblocks subsequent
    Quick-Scan workers (separate connection, file-level lock) — the original
    bug was a leaked open transaction holding the write lock for the rest
    of the session.

    In the strict 1:1 model there is no ``prune_orphan_games`` step:
    ON DELETE CASCADE on ``metadata``, ``covers``, and ``collection_roms``
    handles dependent cleanup automatically when the ``roms`` row is deleted.

    Cancellation is supported per dependent-row chunk via the progress
    callback's :meth:`_check_cancel`. The final ``DELETE FROM roms``
    statement is not interruptible — it runs to completion once dependent
    cleanup is done. That's intentional: an abort mid-delete would leave
    the DB in a state the user can't easily reason about.
    """

    progress = Signal(int, int, str)
    finished_ok = Signal(int, int)

    _operation_name = "Clean Missing Entries"

    def _run_work(self, conn: sqlite3.Connection) -> None:
        def _progress(current: int, total: int, label: str) -> None:
            self._check_cancel()
            self.progress.emit(current, total, label)

        try:
            self.progress.emit(0, 0, "Counting missing entries…")
            total = q.count_missing_roms(conn)
            if total == 0:
                # Nothing to do — emit a clean finish so the dialog can
                # close itself without a "failed" detour.
                self.finished_ok.emit(0, 0)
                return

            self.progress.emit(0, total, "Deleting dependent rows…")
            deleted = q.delete_missing_roms(
                conn, progress_callback=_progress
            )
            # In the strict 1:1 model there is no separate games table to
            # prune.  ON DELETE CASCADE on metadata/covers/collection_roms
            # cleans up dependents automatically when the roms row is deleted.
            pruned = 0
            conn.commit()
            logger.info(
                "CleanMissing complete: deleted_roms=%d pruned_games=%d",
                deleted,
                pruned,
            )
        except Exception:
            # Roll back the implicit transaction so a subsequent worker on a
            # separate connection isn't blocked by a leaked write lock.
            # ``_DbWorker.run`` logs + emits ``failed`` once we re-raise.
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.exception("CleanMissing: rollback after failure errored")
            raise

        self.finished_ok.emit(deleted, pruned)
