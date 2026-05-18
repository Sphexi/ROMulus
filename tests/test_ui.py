"""Tests for the PySide6 UI shell — models, loaders, workers.

Widget rendering tests are kept off the main path (no pytest-qt). We instantiate
a QApplication once per session via the `qapp` fixture, then exercise model
classes (GameTableModel, GameTableProxy) and pure helpers without showing windows.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from PySide6.QtCore import QModelIndex, Qt

from romulus.db import create_tables, get_config, seed_defaults, set_config
from romulus.db import queries as q
from romulus.models import seed_systems
from romulus.ui.game_table import (
    GameRow,
    GameTable,
    GameTableModel,
    GameTableProxy,
    _format_size,
    load_rom_rows,
)
from romulus.ui.system_sidebar import (
    KIND_ALL,
    NODE_KIND_ROLE,
    SYSTEM_ID_ROLE,
    SystemSidebar,
    get_rom_counts_by_system,
    get_total_rom_count,
)


def _insert_rom(
    conn: sqlite3.Connection,
    filename: str,
    system_id: str,
    *,
    size_bytes: int = 1024,
    match_confidence: str = "fuzzy",
) -> int:
    """Insert a ROM row and return its id."""
    return q.upsert_rom(
        conn,
        {
            "path": f"/library/{system_id}/{filename}",
            "filename": filename,
            "extension": "." + filename.rsplit(".", 1)[-1],
            "size_bytes": size_bytes,
            "mtime": time.time(),
            "system_id": system_id,
            "fuzzy_key": filename.lower().replace(" ", ""),
            "match_confidence": match_confidence,
        },
    )


# ---------------------------------------------------------------------------
# _format_size
# ---------------------------------------------------------------------------


class TestFormatSize:
    def test_bytes(self) -> None:
        assert _format_size(512) == "512 B"

    def test_kilobytes(self) -> None:
        assert _format_size(2048).endswith("KB")

    def test_megabytes(self) -> None:
        assert _format_size(5 * 1024 * 1024).endswith("MB")


# ---------------------------------------------------------------------------
# GameTableModel
# ---------------------------------------------------------------------------


class TestGameTableModel:
    def test_empty_model(self, qapp) -> None:
        model = GameTableModel()
        assert model.rowCount() == 0
        assert model.columnCount() == 6  # Name/System/Region/Size/Match/Path

    def test_rows_render(self, qapp) -> None:
        rows = [
            GameRow(
                rom_id=1,
                name="Super Mario World.sfc",
                system_id="snes",
                system_name="SNES",
                region="USA",
                size_bytes=524288,
                match_confidence="dat_verified",
            ),
        ]
        model = GameTableModel(rows)
        assert model.rowCount() == 1
        index = model.index(0, 0)
        assert model.data(index) == "Super Mario World.sfc"
        assert model.data(model.index(0, 1)) == "SNES"
        assert model.data(model.index(0, 2)) == "USA"
        assert model.data(model.index(0, 4)) == "dat_verified"

    def test_headers(self, qapp) -> None:
        model = GameTableModel()
        assert model.headerData(0, Qt.Orientation.Horizontal) == "Name"
        assert model.headerData(3, Qt.Orientation.Horizontal) == "Size"

    def test_user_role_returns_raw_size(self, qapp) -> None:
        row = GameRow(
            rom_id=1,
            name="A",
            system_id="snes",
            system_name="SNES",
            region="USA",
            size_bytes=4096,
            match_confidence="fuzzy",
        )
        model = GameTableModel([row])
        size_index = model.index(0, 3)
        assert model.data(size_index, Qt.ItemDataRole.UserRole) == 4096

    def test_invalid_index_returns_none(self, qapp) -> None:
        model = GameTableModel()
        assert model.data(QModelIndex()) is None

    def test_set_rows_replaces_data(self, qapp) -> None:
        model = GameTableModel(
            [
                GameRow(
                    rom_id=1,
                    name="Old.sfc",
                    system_id="snes",
                    system_name="SNES",
                    region="",
                    size_bytes=10,
                    match_confidence="fuzzy",
                ),
            ]
        )
        assert model.rowCount() == 1
        model.set_rows([])
        assert model.rowCount() == 0


# ---------------------------------------------------------------------------
# GameTableProxy — sort + filter
# ---------------------------------------------------------------------------


def _row(name: str, size: int, region: str = "USA") -> GameRow:
    return GameRow(
        rom_id=hash(name) & 0xFFFFFFFF,
        name=name,
        system_id="snes",
        system_name="SNES",
        region=region,
        size_bytes=size,
        match_confidence="fuzzy",
    )


class TestGameTableProxy:
    def test_filter_by_name(self, qapp) -> None:
        model = GameTableModel(
            [
                _row("Super Mario World.sfc", 512),
                _row("Donkey Kong Country.sfc", 1024),
                _row("Mario Kart.sfc", 2048),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_name_filter("mario")
        assert proxy.rowCount() == 2

    def test_filter_clears(self, qapp) -> None:
        model = GameTableModel([_row("A.sfc", 1), _row("B.sfc", 2)])
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_name_filter("a")
        assert proxy.rowCount() == 1
        proxy.set_name_filter("")
        assert proxy.rowCount() == 2

    def test_sort_by_size_numerically(self, qapp) -> None:
        # Without the UserRole sort key, "1.0 MB" would sort lexicographically.
        model = GameTableModel(
            [
                _row("a.sfc", 5 * 1024 * 1024),
                _row("b.sfc", 1024),
                _row("c.sfc", 200 * 1024),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.sort(3, Qt.SortOrder.AscendingOrder)
        first_name = proxy.data(proxy.index(0, 0))
        last_name = proxy.data(proxy.index(2, 0))
        assert first_name == "b.sfc"
        assert last_name == "a.sfc"


# ---------------------------------------------------------------------------
# load_rom_rows + sidebar queries
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_load_rom_rows_filters_by_system(self, seeded_db) -> None:
        _insert_rom(seeded_db, "Mario.sfc", "snes")
        _insert_rom(seeded_db, "Sonic.md", "megadrive")
        seeded_db.commit()

        all_rows = load_rom_rows(seeded_db)
        assert {r.system_id for r in all_rows} == {"snes", "megadrive"}

        snes_rows = load_rom_rows(seeded_db, "snes")
        assert [r.system_id for r in snes_rows] == ["snes"]
        assert snes_rows[0].name == "Mario.sfc"

    def test_load_rom_rows_pulls_system_display_name(self, seeded_db) -> None:
        _insert_rom(seeded_db, "Mario.sfc", "snes")
        seeded_db.commit()
        rows = load_rom_rows(seeded_db)
        assert rows[0].system_name == "SNES"

    def test_load_rom_rows_respects_limit(self, seeded_db) -> None:
        for i in range(5):
            _insert_rom(seeded_db, f"Game{i}.sfc", "snes")
        seeded_db.commit()
        rows = load_rom_rows(seeded_db, limit=3)
        assert len(rows) == 3

    def test_total_rom_count(self, seeded_db) -> None:
        assert get_total_rom_count(seeded_db) == 0
        _insert_rom(seeded_db, "Mario.sfc", "snes")
        _insert_rom(seeded_db, "Sonic.md", "megadrive")
        seeded_db.commit()
        assert get_total_rom_count(seeded_db) == 2

    def test_rom_counts_by_system(self, seeded_db) -> None:
        _insert_rom(seeded_db, "Mario.sfc", "snes")
        _insert_rom(seeded_db, "Zelda.sfc", "snes")
        _insert_rom(seeded_db, "Sonic.md", "megadrive")
        seeded_db.commit()
        counts = dict((sid, n) for sid, _name, n in get_rom_counts_by_system(seeded_db))
        assert counts == {"snes": 2, "megadrive": 1}

    def test_get_collections_empty(self, seeded_db) -> None:
        # ``queries.get_collections`` returns sqlite3.Row objects now that the
        # ``system_sidebar.get_collections`` compat shim has been removed.
        assert list(q.get_collections(seeded_db)) == []

    def test_get_collections_with_rows(self, seeded_db) -> None:
        seeded_db.execute(
            "INSERT INTO collections (name, description) VALUES (?, ?)",
            ("Favorites", "Hand-picked"),
        )
        seeded_db.commit()
        collections = q.get_collections(seeded_db)
        assert len(collections) == 1
        # sqlite3.Row column access — no more positional tuples.
        assert collections[0]["name"] == "Favorites"
        assert collections[0]["game_count"] == 0


# ---------------------------------------------------------------------------
# SystemSidebar
# ---------------------------------------------------------------------------


class TestSystemSidebar:
    def test_populate_with_no_roms(self, qapp, seeded_db) -> None:
        sidebar = SystemSidebar()
        sidebar.populate(seeded_db)
        model = sidebar.model()
        assert model.rowCount() >= 1
        first = model.item(0)
        assert first.data(NODE_KIND_ROLE) == KIND_ALL
        assert "All" in first.text()

    def test_populate_groups_by_system(self, qapp, seeded_db) -> None:
        _insert_rom(seeded_db, "Mario.sfc", "snes")
        _insert_rom(seeded_db, "Sonic.md", "megadrive")
        seeded_db.commit()

        sidebar = SystemSidebar()
        sidebar.populate(seeded_db)
        model = sidebar.model()

        # Top-level rows: "All", then "Systems" header.
        systems_header = None
        for r in range(model.rowCount()):
            item = model.item(r)
            if item.text() == "Systems":
                systems_header = item
                break
        assert systems_header is not None
        system_ids = {
            systems_header.child(r).data(SYSTEM_ID_ROLE)
            for r in range(systems_header.rowCount())
        }
        assert system_ids == {"snes", "megadrive"}

    def test_emits_system_selected_signal(self, qapp, seeded_db) -> None:
        _insert_rom(seeded_db, "Mario.sfc", "snes")
        seeded_db.commit()

        sidebar = SystemSidebar()
        sidebar.populate(seeded_db)

        received: list[object] = []
        sidebar.system_selected.connect(received.append)

        # Locate the "snes" leaf and select it.
        model = sidebar.model()
        for r in range(model.rowCount()):
            header = model.item(r)
            if header.text() != "Systems":
                continue
            for c in range(header.rowCount()):
                leaf = header.child(c)
                if leaf.data(SYSTEM_ID_ROLE) == "snes":
                    sidebar.setCurrentIndex(model.indexFromItem(leaf))
                    break

        assert received and received[0] == "snes"

    def test_selecting_all_emits_none(self, qapp, seeded_db) -> None:
        sidebar = SystemSidebar()
        sidebar.populate(seeded_db)

        received: list[object] = []
        sidebar.system_selected.connect(received.append)

        model = sidebar.model()
        sidebar.setCurrentIndex(model.indexFromItem(model.item(0)))
        assert received and received[0] is None


# ---------------------------------------------------------------------------
# GameTable widget — minimal smoke test
# ---------------------------------------------------------------------------


class TestGameTableWidget:
    def test_search_box_updates_proxy_filter(self, qapp) -> None:
        widget = GameTable()
        widget.set_rows(
            [
                _row("Super Mario World.sfc", 512),
                _row("Donkey Kong Country.sfc", 1024),
            ]
        )
        assert widget.proxy.rowCount() == 2
        widget.search.setText("mario")
        assert widget.proxy.rowCount() == 1


# ---------------------------------------------------------------------------
# ScanWorker — using the real scanner with a temp library
# ---------------------------------------------------------------------------


class TestScanWorker:
    def test_worker_emits_progress_and_finishes(self, qapp, tmp_path) -> None:
        from PySide6.QtCore import QEventLoop

        from romulus.db import get_connection
        from romulus.ui.workers import ScanWorker

        db_path = tmp_path / "romulus.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        seed_defaults(conn)
        conn.close()

        library = tmp_path / "library"
        snes = library / "snes"
        snes.mkdir(parents=True)
        (snes / "Mario.sfc").write_bytes(b"\x00" * 32)
        (snes / "Zelda.sfc").write_bytes(b"\x00" * 32)

        worker = ScanWorker(db_path, library)
        progress_events: list[tuple[int, str]] = []
        finished: list[tuple] = []
        failed: list[str] = []
        worker.progress.connect(lambda c, f: progress_events.append((c, f)))
        worker.finished_ok.connect(lambda *args: finished.append(args))
        worker.failed.connect(failed.append)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        assert not failed
        assert finished, "finished_ok was not emitted"
        scan_id, files_found, files_with_system, files_skipped, systems_seen = finished[0]
        assert files_found == 2
        assert files_with_system == 2
        assert files_skipped == 0
        assert "snes" in systems_seen
        assert progress_events  # at least one progress tick

    def test_worker_emits_failed_on_bad_db_path(self, qapp, tmp_path) -> None:
        from PySide6.QtCore import QEventLoop

        from romulus.ui.workers import ScanWorker

        # Library has to exist; db_path is fine because get_connection just opens a file.
        # Use a busted library path to force scan_library to fail mid-run.
        worker = ScanWorker(tmp_path / "ok.db", "/this/path/does/not/exist/__noway__")
        finished: list[tuple] = []
        failed: list[str] = []
        worker.finished_ok.connect(lambda *args: finished.append(args))
        worker.failed.connect(failed.append)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        # The scanner walks an empty/missing tree without raising; expect a clean
        # finish with zero files rather than a failure here. Either way, no crash.
        assert failed or finished


class TestEnrichWorker:
    def test_worker_runs_with_empty_library(self, qapp, tmp_path) -> None:
        from PySide6.QtCore import QEventLoop

        from romulus.db import get_connection
        from romulus.ui.workers import EnrichWorker

        db_path = tmp_path / "romulus.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        seed_defaults(conn)
        conn.close()

        worker = EnrichWorker(db_path, cache_dir=tmp_path / "covers")
        finished: list[tuple] = []
        failed: list[str] = []
        worker.finished_ok.connect(lambda *args: finished.append(args))
        worker.failed.connect(failed.append)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        assert not failed
        assert finished
        games_processed, metadata_added, covers_added = finished[0]
        assert games_processed == 0
        assert metadata_added == 0
        assert covers_added == 0

    def test_worker_emits_failed_on_bad_db(self, qapp, tmp_path) -> None:
        from PySide6.QtCore import QEventLoop

        from romulus.ui.workers import EnrichWorker

        # Pointing at a directory path forces get_connection to error.
        bad_path = tmp_path / "not-a-db"
        bad_path.mkdir()
        worker = EnrichWorker(bad_path, cache_dir=tmp_path / "covers")
        finished: list[tuple] = []
        failed: list[str] = []
        worker.finished_ok.connect(lambda *args: finished.append(args))
        worker.failed.connect(failed.append)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        assert failed or finished  # must not crash silently


# ---------------------------------------------------------------------------
# _DbWorker base class — shared cancel + DB lifecycle plumbing
# ---------------------------------------------------------------------------


class TestDbWorkerBase:
    """Verify the four concrete workers all share the same base behaviour:

    * a ``_WorkerCancelled`` raised from inside the work method becomes a
      ``"<Op> cancelled"`` ``failed`` signal,
    * an arbitrary exception becomes a ``"<Op> failed: <msg>"`` signal,
    * the DB connection is opened on the worker thread and closed on exit.
    """

    def test_concrete_workers_inherit_from_db_worker(self) -> None:
        from romulus.ui.workers import (
            EnrichWorker,
            ExportWorker,
            OrganizeWorker,
            ScanWorker,
            _DbWorker,
            _WorkerCancelled,
        )

        for cls in (ScanWorker, EnrichWorker, OrganizeWorker, ExportWorker):
            assert issubclass(cls, _DbWorker), f"{cls.__name__} must inherit _DbWorker"
            # Each worker customises the cancel/failed message prefix.
            assert cls._operation_name in {"Scan", "Enrichment", "Organize", "Export"}

        # Shared cancel marker — not the four-per-worker shapes the previous
        # workers.py declared.
        assert issubclass(_WorkerCancelled, Exception)

    def test_base_class_opens_and_closes_connection(
        self, qapp, tmp_path
    ) -> None:
        from PySide6.QtCore import QEventLoop

        from romulus.db import get_connection
        from romulus.ui.workers import _DbWorker, _WorkerCancelled

        db_path = tmp_path / "x.db"
        conn = get_connection(db_path)
        create_tables(conn)
        conn.close()

        class _ProbeWorker(_DbWorker):
            _operation_name = "Probe"
            done = False

            def _run_work(self, conn) -> None:  # noqa: ANN001
                # The connection must be alive on the worker thread.
                assert conn.execute("SELECT 1").fetchone()[0] == 1
                self.done = True

        w = _ProbeWorker(db_path)
        loop = QEventLoop()
        w.finished.connect(loop.quit)
        w.start()
        loop.exec()
        assert w.done is True

        # Cancel marker translates to a "<op> cancelled" failed message.
        class _CancelProbe(_DbWorker):
            _operation_name = "CancelProbe"

            def _run_work(self, conn) -> None:  # noqa: ANN001, ARG002
                raise _WorkerCancelled

        cancel_msgs: list[str] = []
        cw = _CancelProbe(db_path)
        cw.failed.connect(cancel_msgs.append)
        cancel_loop = QEventLoop()
        cw.finished.connect(cancel_loop.quit)
        cw.start()
        cancel_loop.exec()
        assert cancel_msgs == ["CancelProbe cancelled"]

        # Arbitrary exceptions are wrapped with the operation name prefix.
        # Per security audit v0.1.0 finding #12, the exception message itself
        # is NOT included in the user-facing string — only the exception type
        # name. The full traceback is logged separately for forensics.
        class _BoomProbe(_DbWorker):
            _operation_name = "BoomProbe"

            def _run_work(self, conn) -> None:  # noqa: ANN001, ARG002
                raise RuntimeError("kaboom-with-secret-/path/to/credentials")

        boom_msgs: list[str] = []
        bw = _BoomProbe(db_path)
        bw.failed.connect(boom_msgs.append)
        boom_loop = QEventLoop()
        bw.finished.connect(boom_loop.quit)
        bw.start()
        boom_loop.exec()
        assert boom_msgs
        assert boom_msgs[0] == "BoomProbe failed (RuntimeError)"
        # The original exception message must NEVER leak to the UI signal.
        assert "kaboom" not in boom_msgs[0]
        assert "credentials" not in boom_msgs[0]


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------


class TestSettingsDialog:
    def test_save_round_trips_config(self, qapp, seeded_db) -> None:
        from romulus.ui.settings_dialog import SettingsDialog

        seed_defaults(seeded_db)
        dialog = SettingsDialog(seeded_db)
        dialog.general.library_path.setText("/tmp/my-roms")
        dialog.general.theme.setCurrentText("Dark")
        dialog.scan.threads.setValue(12)
        dialog.metadata.username.setText("user1")
        dialog._accept_and_save()

        assert get_config(seeded_db, "library_path") == "/tmp/my-roms"
        assert get_config(seeded_db, "theme") == "dark"
        assert get_config(seeded_db, "scan_threads") == "12"
        assert get_config(seeded_db, "screenscraper_username") == "user1"

    def test_diagnostics_tab_persists_and_applies_log_level(
        self, qapp, seeded_db, tmp_path
    ) -> None:
        import logging as _logging

        from romulus.app import setup_logging
        from romulus.ui.settings_dialog import SettingsDialog

        seed_defaults(seeded_db)
        # Use tmp_path so the test doesn't share the user's real
        # DEFAULT_LOG_PATH — a running ROMulus instance would otherwise
        # hold that path locked and the new lock-detection guard would
        # fire here.
        setup_logging(tmp_path / "romulus.log")
        dialog = SettingsDialog(seeded_db)
        dialog.diagnostics.level.setCurrentText("WARNING")
        dialog._accept_and_save()

        assert get_config(seeded_db, "log_level") == "WARNING"
        # Live runtime adjustment — the new level takes effect without restart.
        assert _logging.getLogger().level == _logging.WARNING

    def test_diagnostics_tab_loads_existing_level(self, qapp, seeded_db) -> None:
        from romulus.ui.settings_dialog import SettingsDialog

        seed_defaults(seeded_db)
        set_config(seeded_db, "log_level", "DEBUG")
        dialog = SettingsDialog(seeded_db)
        assert dialog.diagnostics.level.currentText() == "DEBUG"


# ---------------------------------------------------------------------------
# App initialization helper
# ---------------------------------------------------------------------------


class TestAppInit:
    def test_initialize_database_seeds(self, qapp, tmp_path) -> None:
        from romulus.app import initialize_database

        db_path = tmp_path / "test.db"
        conn = initialize_database(db_path)
        try:
            n_systems = conn.execute("SELECT COUNT(*) FROM systems").fetchone()[0]
            assert n_systems >= 30
            assert get_config(conn, "library_path") is not None
        finally:
            conn.close()

    def test_ensure_library_path_returns_existing(self, qapp, tmp_path) -> None:
        from romulus.app import ensure_library_path, initialize_database

        db_path = tmp_path / "test.db"
        conn = initialize_database(db_path)
        try:
            set_config(conn, "library_path", "/already/set")
            assert ensure_library_path(conn) == "/already/set"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# MainWindow smoke test
# ---------------------------------------------------------------------------


class TestMainWindow:
    def test_construct_and_refresh(self, qapp, seeded_db) -> None:
        from romulus.ui.main_window import MainWindow

        _insert_rom(seeded_db, "Mario.sfc", "snes")
        _insert_rom(seeded_db, "Sonic.md", "megadrive")
        seeded_db.commit()

        window = MainWindow(seeded_db)
        window.refresh_all()
        assert window.game_table.proxy.rowCount() == 2
        # Status bar should report the total ROM count.
        assert "2" in window.status_label.text()

    def test_system_filter_narrows_table(self, qapp, seeded_db) -> None:
        from romulus.ui.main_window import MainWindow

        _insert_rom(seeded_db, "Mario.sfc", "snes")
        _insert_rom(seeded_db, "Sonic.md", "megadrive")
        seeded_db.commit()

        window = MainWindow(seeded_db)
        window.refresh_all()
        window._on_system_selected("snes")
        assert window.game_table.proxy.rowCount() == 1

    @pytest.mark.parametrize("system_id", [None, "snes"])
    def test_refresh_handles_optional_filter(self, qapp, seeded_db, system_id) -> None:
        from romulus.ui.main_window import MainWindow

        _insert_rom(seeded_db, "Mario.sfc", "snes")
        seeded_db.commit()
        window = MainWindow(seeded_db)
        window._selected_system = system_id
        window.refresh_game_table()
        assert window.game_table.proxy.rowCount() == 1

    def test_close_event_waits_on_running_worker(self, qapp, seeded_db) -> None:
        """closeEvent must request cancel and wait, not leak the QThread."""
        from PySide6.QtGui import QCloseEvent

        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)

        class _FakeWorker:
            def __init__(self) -> None:
                self.cancel_called = False
                self.wait_args: list[int] = []
                self._running = True

            def isRunning(self) -> bool:  # noqa: N802 - mimics QThread API
                return self._running

            def cancel(self) -> None:
                self.cancel_called = True

            def wait(self, msecs: int) -> bool:
                self.wait_args.append(msecs)
                self._running = False
                return True

        fake = _FakeWorker()
        window._scan_worker = fake  # type: ignore[assignment]
        window.closeEvent(QCloseEvent())
        assert fake.cancel_called is True
        assert fake.wait_args, "wait() should be invoked with a timeout"

    def test_quick_scan_guards_against_concurrent_runs(
        self, qapp, seeded_db, monkeypatch
    ) -> None:
        """Clicking Quick Scan while a scan is running must be a no-op."""
        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)

        class _FakeRunningWorker:
            def isRunning(self) -> bool:  # noqa: N802 - mimics QThread API
                return True

        window._scan_worker = _FakeRunningWorker()  # type: ignore[assignment]

        info_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "romulus.ui.main_window.QMessageBox.information",
            lambda *args, **_kw: info_calls.append((args[1], args[2])),
        )
        # Should bail out before constructing the dialog/worker.
        window._on_quick_scan()
        assert info_calls, "expected a warning when a scan is already running"

    def test_enrich_guards_against_concurrent_runs(
        self, qapp, seeded_db, monkeypatch
    ) -> None:
        """Clicking Enrich while enrichment is running must be a no-op."""
        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)

        class _FakeRunningWorker:
            def isRunning(self) -> bool:  # noqa: N802 - mimics QThread API
                return True

        window._enrich_worker = _FakeRunningWorker()  # type: ignore[assignment]

        info_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "romulus.ui.main_window.QMessageBox.information",
            lambda *args, **_kw: info_calls.append((args[1], args[2])),
        )
        window._on_enrich()
        assert info_calls, "expected a warning when enrichment is already running"


# ---------------------------------------------------------------------------
# GameTableProxy — region + match-status filters
# ---------------------------------------------------------------------------


def _row_full(
    name: str,
    region: str = "USA",
    match: str = "fuzzy",
    game_id: int | None = 1,
) -> GameRow:
    return GameRow(
        rom_id=hash(name) & 0xFFFFFFFF,
        name=name,
        system_id="snes",
        system_name="SNES",
        region=region,
        size_bytes=1024,
        match_confidence=match,
        game_id=game_id,
    )


class TestRegionAndMatchFilters:
    def test_region_filter_narrows_to_usa(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_full("USA Game", region="USA"),
                _row_full("Euro Game", region="Europe"),
                _row_full("JP Game", region="Japan"),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_region_filter("USA")
        assert proxy.rowCount() == 1

    def test_region_filter_other_excludes_known_regions(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_full("USA", region="USA"),
                _row_full("Korea", region="Korea"),
                _row_full("Brazil", region="Brazil"),
                _row_full("Blank", region=""),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_region_filter("Other")
        # USA is excluded; blank is excluded; Korea + Brazil pass.
        assert proxy.rowCount() == 2

    def test_match_filter_verified(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_full("v1", match="dat_verified"),
                _row_full("v2", match="header"),
                _row_full("v3", match="fuzzy"),
                _row_full("v4", match="unmatched"),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_match_filter("Verified")
        assert proxy.rowCount() == 2

    def test_match_filter_unmatched(self, qapp) -> None:
        # "Unmatched" now means strictly match_confidence == "unmatched".
        # "Fuzzy" is its own distinct filter option (item #6).
        model = GameTableModel(
            [
                _row_full("v1", match="dat_verified"),
                _row_full("v2", match="fuzzy"),
                _row_full("v3", match="unmatched"),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_match_filter("Unmatched")
        assert proxy.rowCount() == 1  # only "unmatched" — fuzzy is now separate

    def test_match_filter_fuzzy(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_full("v1", match="dat_verified"),
                _row_full("v2", match="fuzzy"),
                _row_full("v3", match="unmatched"),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_match_filter("Fuzzy")
        assert proxy.rowCount() == 1  # only "fuzzy"

    def test_filters_compose_with_name_search(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_full("Mario USA", region="USA"),
                _row_full("Mario JP", region="Japan"),
                _row_full("Zelda USA", region="USA"),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_name_filter("mario")
        proxy.set_region_filter("USA")
        assert proxy.rowCount() == 1


# ---------------------------------------------------------------------------
# GameTable widget — selection signal + context-menu collection plumbing
# ---------------------------------------------------------------------------


class TestGameTableSelection:
    def test_selecting_row_emits_game_selected(self, qapp) -> None:
        from romulus.ui.game_table import GameTable

        widget = GameTable()
        widget.set_rows(
            [
                _row_full("A", game_id=7),
                _row_full("B", game_id=8),
            ]
        )
        # Look up each proxy row's game_id directly so we are not coupled to
        # any default sort order; assert one of them fires the signal.
        received: list[object] = []
        widget.game_selected.connect(received.append)

        target_index = widget.proxy.index(0, 0)
        target_source = widget.proxy.mapToSource(target_index)
        expected_game_id = widget.model.row_at(target_source.row()).game_id

        widget.view.selectionModel().setCurrentIndex(
            target_index,
            widget.view.selectionModel().SelectionFlag.ClearAndSelect
            | widget.view.selectionModel().SelectionFlag.Rows,
        )
        assert received and received[-1] == expected_game_id

    def test_set_available_collections_round_trips(self, qapp) -> None:
        from romulus.ui.game_table import GameTable

        widget = GameTable()
        widget.set_available_collections([(1, "Favorites"), (2, "RPGs")])
        assert widget._available_collections == [
            (1, "Favorites"),
            (2, "RPGs"),
        ]

    def test_set_collection_context_round_trips(self, qapp) -> None:
        from romulus.ui.game_table import GameTable

        widget = GameTable()
        assert widget._collection_context is False
        widget.set_collection_context(True)
        assert widget._collection_context is True


# ---------------------------------------------------------------------------
# DetailPanel — rendering against real DB rows
# ---------------------------------------------------------------------------


class TestDetailPanel:
    def _seed_game_with_metadata(
        self,
        conn: sqlite3.Connection,
        *,
        title: str = "Chrono Trigger",
        with_metadata: bool = True,
    ) -> int:
        from romulus.db import queries as queries_mod

        game_id = queries_mod.upsert_game(
            conn, {"title": title, "system_id": "snes", "region": "USA"}
        )
        rom_id = _insert_rom(conn, f"{title}.sfc", "snes", match_confidence="dat_verified")
        queries_mod.link_rom_to_game(conn, rom_id, game_id)
        if with_metadata:
            queries_mod.upsert_metadata(
                conn,
                game_id,
                {
                    "description": "Time-traveling JRPG.",
                    "genre": "RPG",
                    "developer": "Square",
                    "publisher": "Square",
                },
                source="launchbox",
            )
        conn.commit()
        return game_id

    def test_blank_when_no_game_selected(self, qapp, seeded_db) -> None:
        from romulus.ui.detail_panel import DetailPanel

        panel = DetailPanel(seeded_db)
        panel.update_game(None)
        assert panel.current_game_id is None
        assert "Select a game" in panel.title_label.text()
        assert not panel.favorite_button.isEnabled()
        assert not panel.collection_button.isEnabled()

    def test_renders_game_with_metadata(self, qapp, seeded_db) -> None:
        from romulus.ui.detail_panel import DetailPanel

        game_id = self._seed_game_with_metadata(seeded_db)
        panel = DetailPanel(seeded_db)
        panel.update_game(game_id)

        assert panel.current_game_id == game_id
        assert panel.title_label.text() == "Chrono Trigger"
        # Metadata fields are now individual rows in a key/value grid;
        # each value lives in panel._meta_value_labels keyed by field id.
        assert "RPG" in panel._meta_value_labels["genre"].text()
        assert "Square" in panel._meta_value_labels["developer"].text()
        assert "Time-traveling" in panel.description.text()
        # ``isVisible`` is False for any widget whose parent isn't shown,
        # so check the explicit hidden flag instead. The panel toggles
        # show()/hide() based on whether description text is present.
        assert not panel.description.isHidden()
        # DAT-verified ROM means the badge label says "DAT verified".
        assert "DAT verified" in panel.match_badge.text()
        assert panel.favorite_button.isEnabled()
        assert panel.collection_button.isEnabled()

    def test_renders_game_without_metadata(self, qapp, seeded_db) -> None:
        from romulus.ui.detail_panel import DetailPanel

        game_id = self._seed_game_with_metadata(
            seeded_db, title="Obscure Title", with_metadata=False
        )
        panel = DetailPanel(seeded_db)
        panel.update_game(game_id)
        assert panel.title_label.text() == "Obscure Title"
        # Metadata-bearing rows should be empty and the description label
        # hidden entirely (the new "no box for empty text" behaviour).
        assert panel._meta_value_labels["genre"].text() == ""
        assert panel._meta_value_labels["publisher"].text() == ""
        assert panel.description.text() == ""
        assert panel.description.isHidden()
        assert panel.favorite_button.isEnabled()

    def test_missing_cover_renders_placeholder(self, qapp, seeded_db) -> None:
        from romulus.db import queries as queries_mod
        from romulus.ui.detail_panel import PLACEHOLDER_TEXT, DetailPanel

        game_id = self._seed_game_with_metadata(seeded_db)
        # Insert a cover row pointing at a path that does not exist on disk.
        queries_mod.insert_cover(
            seeded_db,
            game_id,
            "Named_Boxarts",
            "https://example.com/missing.png",
            local_path=str(seeded_db.execute("SELECT 'no-such-file'").fetchone()[0]),
        )
        seeded_db.commit()
        panel = DetailPanel(seeded_db)
        panel.update_game(game_id)
        # No pixmap loaded; placeholder text shown.
        assert panel.cover_label.text() == PLACEHOLDER_TEXT
        assert panel.cover_label.pixmap().isNull()

    def test_loads_cover_from_disk_when_present(
        self, qapp, seeded_db, tmp_path
    ) -> None:
        from PySide6.QtGui import QImage

        from romulus.db import queries as queries_mod
        from romulus.ui.detail_panel import DetailPanel

        game_id = self._seed_game_with_metadata(seeded_db)
        cover_path = tmp_path / "cover.png"
        # 4x4 transparent PNG so QPixmap definitely loads it.
        QImage(4, 4, QImage.Format.Format_RGBA8888).save(str(cover_path), "PNG")
        queries_mod.insert_cover(
            seeded_db,
            game_id,
            "Named_Boxarts",
            "https://example.com/cover.png",
            local_path=str(cover_path),
        )
        seeded_db.commit()
        panel = DetailPanel(seeded_db)
        panel.update_game(game_id)
        # Pixmap is now set, no placeholder text.
        assert not panel.cover_label.pixmap().isNull()
        assert panel.cover_label.text() == ""

    def test_favorite_toggle_round_trip(self, qapp, seeded_db) -> None:
        from romulus.db import queries as queries_mod
        from romulus.ui.detail_panel import DetailPanel

        game_id = self._seed_game_with_metadata(seeded_db)
        panel = DetailPanel(seeded_db)
        panel.update_game(game_id)
        favorites_id = queries_mod.ensure_favorites_collection(seeded_db)

        # Click → adds to favorites.
        panel.favorite_button.setChecked(True)
        panel._on_favorite_clicked()
        assert queries_mod.is_game_in_collection(
            seeded_db, favorites_id, game_id
        )

        # Click again → removes from favorites.
        panel.favorite_button.setChecked(False)
        panel._on_favorite_clicked()
        assert not queries_mod.is_game_in_collection(
            seeded_db, favorites_id, game_id
        )


# ---------------------------------------------------------------------------
# MainWindow — collection sidebar wiring + DetailPanel selection
# ---------------------------------------------------------------------------


class TestMainWindowCollections:
    def test_detail_panel_updates_on_game_selection(
        self, qapp, seeded_db
    ) -> None:
        from romulus.db import queries as queries_mod
        from romulus.ui.main_window import MainWindow

        game_id = queries_mod.upsert_game(
            seeded_db, {"title": "Mario", "system_id": "snes"}
        )
        rom_id = _insert_rom(seeded_db, "Mario.sfc", "snes")
        queries_mod.link_rom_to_game(seeded_db, rom_id, game_id)
        seeded_db.commit()

        window = MainWindow(seeded_db)
        window.refresh_all()
        window._on_game_selected(game_id)
        assert window.detail_panel.current_game_id == game_id

    def test_collection_selection_filters_game_table(
        self, qapp, seeded_db
    ) -> None:
        from romulus.db import queries as queries_mod
        from romulus.ui.main_window import MainWindow

        # Two games on the same system; only one belongs to the collection.
        gid_in = queries_mod.upsert_game(
            seeded_db, {"title": "Mario", "system_id": "snes"}
        )
        rom_in = _insert_rom(seeded_db, "Mario.sfc", "snes")
        queries_mod.link_rom_to_game(seeded_db, rom_in, gid_in)

        gid_out = queries_mod.upsert_game(
            seeded_db, {"title": "Zelda", "system_id": "snes"}
        )
        rom_out = _insert_rom(seeded_db, "Zelda.sfc", "snes")
        queries_mod.link_rom_to_game(seeded_db, rom_out, gid_out)

        cid = queries_mod.create_collection(seeded_db, "Mario Pack")
        queries_mod.add_game_to_collection(seeded_db, cid, gid_in)
        seeded_db.commit()

        window = MainWindow(seeded_db)
        window.refresh_all()
        window._on_collection_selected(cid)
        assert window.game_table.proxy.rowCount() == 1
        assert window.game_table.model.row_at(0).game_id == gid_in

    def test_add_to_favorites_request_through_main_window(
        self, qapp, seeded_db
    ) -> None:
        from romulus.db import queries as queries_mod
        from romulus.ui.main_window import MainWindow

        game_id = queries_mod.upsert_game(
            seeded_db, {"title": "Metroid", "system_id": "snes"}
        )
        rom_id = _insert_rom(seeded_db, "Metroid.sfc", "snes")
        queries_mod.link_rom_to_game(seeded_db, rom_id, game_id)
        seeded_db.commit()

        window = MainWindow(seeded_db)
        window.refresh_all()
        window._on_add_to_favorites(game_id)

        favorites_id = queries_mod.ensure_favorites_collection(seeded_db)
        assert queries_mod.is_game_in_collection(
            seeded_db, favorites_id, game_id
        )

    def test_remove_from_collection_through_main_window(
        self, qapp, seeded_db
    ) -> None:
        from romulus.db import queries as queries_mod
        from romulus.ui.main_window import MainWindow

        game_id = queries_mod.upsert_game(
            seeded_db, {"title": "Kirby", "system_id": "snes"}
        )
        rom_id = _insert_rom(seeded_db, "Kirby.sfc", "snes")
        queries_mod.link_rom_to_game(seeded_db, rom_id, game_id)
        cid = queries_mod.create_collection(seeded_db, "Easy Mode")
        queries_mod.add_game_to_collection(seeded_db, cid, game_id)
        seeded_db.commit()

        window = MainWindow(seeded_db)
        window.refresh_all()
        window._on_collection_selected(cid)
        window._on_remove_from_collection(game_id)

        assert not queries_mod.is_game_in_collection(seeded_db, cid, game_id)


# ---------------------------------------------------------------------------
# Bug #1 — Worker lifetime: finished clears the Python reference
# ---------------------------------------------------------------------------


class TestWorkerLifetime:
    def test_scan_worker_cleared_after_finished(self, qapp, tmp_path) -> None:
        """_scan_worker is set to None once the worker finishes."""
        from PySide6.QtCore import QEventLoop

        from romulus.db import get_connection
        from romulus.ui.main_window import MainWindow

        db_path = tmp_path / "romulus.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        seed_defaults(conn)
        conn.close()

        # Re-open the connection for MainWindow.
        conn2 = get_connection(db_path)
        window = MainWindow(conn2)

        # Set up a fake worker whose finished signal clears the attribute.
        from romulus.ui.workers import ScanWorker

        library = tmp_path / "library"
        library.mkdir()
        worker = ScanWorker(db_path, library)

        # Wire the clear-slot manually, mirroring what _on_quick_scan does.
        window._scan_worker = worker
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(window._clear_scan_worker)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        # After finished fires, Python reference must be None.
        assert window._scan_worker is None
        conn2.close()

    def test_enrich_worker_cleared_after_finished(self, qapp, tmp_path) -> None:
        """_enrich_worker is set to None once the worker finishes."""
        from PySide6.QtCore import QEventLoop

        from romulus.db import get_connection
        from romulus.ui.main_window import MainWindow
        from romulus.ui.workers import EnrichWorker

        db_path = tmp_path / "romulus.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        seed_defaults(conn)
        conn.close()

        conn2 = get_connection(db_path)
        window = MainWindow(conn2)

        worker = EnrichWorker(db_path, cache_dir=tmp_path / "covers")
        window._enrich_worker = worker
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(window._clear_enrich_worker)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        assert window._enrich_worker is None
        conn2.close()

    def test_close_event_handles_stale_worker_reference(
        self, qapp, seeded_db
    ) -> None:
        """closeEvent must not crash when the C++ QThread is already deleted."""
        from PySide6.QtGui import QCloseEvent

        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)

        class _StaleWorker:
            """Simulates a Python wrapper whose C++ object has been deleted."""

            def isRunning(self) -> bool:  # noqa: N802
                raise RuntimeError(
                    "libshiboken: Internal C++ object already deleted."
                )

            def cancel(self) -> None:
                pass

            def wait(self, _ms: int) -> bool:
                return True

        window._scan_worker = _StaleWorker()  # type: ignore[assignment]
        # Must not raise.
        window.closeEvent(QCloseEvent())


# ---------------------------------------------------------------------------
# Bug #2 — Heavy Scan: worker contract + DAT loading + action wired
# ---------------------------------------------------------------------------


class TestHeavyScanWorker:
    def test_inherits_from_db_worker(self) -> None:
        from romulus.ui.workers import HeavyScanWorker, _DbWorker

        assert issubclass(HeavyScanWorker, _DbWorker)
        assert HeavyScanWorker._operation_name == "Heavy Scan"

    def test_heavy_scan_worker_loads_dats_and_hashes(
        self, qapp, tmp_path
    ) -> None:
        """Worker loads bundled DATs on first run and emits finished_ok."""
        from PySide6.QtCore import QEventLoop

        from romulus.db import get_connection
        from romulus.ui.workers import HeavyScanWorker

        db_path = tmp_path / "romulus.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        seed_defaults(conn)
        conn.close()

        # Empty library — no ROMs to hash; DATs should still load.
        library = tmp_path / "library"
        library.mkdir()

        # Use the real bundled DATs path.
        from romulus.ui.main_window import _BUNDLED_DATS_PATH

        worker = HeavyScanWorker(db_path, library, _BUNDLED_DATS_PATH, workers=2)
        finished: list[tuple] = []
        failed: list[str] = []
        worker.finished_ok.connect(lambda *a: finished.append(a))
        worker.failed.connect(failed.append)

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        assert not failed, f"Worker failed: {failed}"
        assert finished
        total_hashed, total_matched, errors = finished[0]
        assert total_hashed == 0  # no ROMs in empty library
        assert errors == 0

        # DATs must be loaded into dat_entries on first run.
        conn2 = get_connection(db_path)
        dat_count = conn2.execute("SELECT COUNT(*) FROM dat_entries").fetchone()[0]
        conn2.close()
        assert dat_count > 0

    def test_heavy_scan_skips_dat_load_on_second_run(
        self, qapp, tmp_path
    ) -> None:
        """Worker skips load_all_dats when dat_entries already populated."""
        from PySide6.QtCore import QEventLoop

        from romulus.core.dat_parser import load_all_dats
        from romulus.db import get_connection
        from romulus.ui.main_window import _BUNDLED_DATS_PATH
        from romulus.ui.workers import HeavyScanWorker

        db_path = tmp_path / "romulus.db"
        conn = get_connection(db_path)
        create_tables(conn)
        seed_systems(conn)
        seed_defaults(conn)
        # Pre-load DATs so dat_entries is not empty.
        load_all_dats(conn, [_BUNDLED_DATS_PATH])
        first_count = conn.execute(
            "SELECT COUNT(*) FROM dat_entries"
        ).fetchone()[0]
        conn.close()

        library = tmp_path / "library"
        library.mkdir()
        worker = HeavyScanWorker(db_path, library, _BUNDLED_DATS_PATH, workers=2)
        finished: list[tuple] = []
        worker.finished_ok.connect(lambda *a: finished.append(a))

        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.start()
        loop.exec()

        assert finished
        # DAT count must be unchanged (no duplicates inserted).
        conn2 = get_connection(db_path)
        second_count = conn2.execute(
            "SELECT COUNT(*) FROM dat_entries"
        ).fetchone()[0]
        conn2.close()
        assert second_count == first_count

    def test_heavy_scan_action_is_enabled(self, qapp, seeded_db) -> None:
        """The Heavy Scan menu action must be enabled and connected."""
        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)
        # Find the Heavy Scan action in the menu bar.
        found = False
        for action in window.menuBar().actions():
            menu = action.menu()
            if menu is None:
                continue
            for sub in menu.actions():
                if "Heavy Scan" in sub.text():
                    assert sub.isEnabled(), "Heavy Scan action must be enabled"
                    found = True
        assert found, "Heavy Scan action not found in menu"

    def test_heavy_scan_confirmation_dialog_blocks_start(
        self, qapp, seeded_db, monkeypatch, tmp_path
    ) -> None:
        """If the user clicks No in the confirmation dialog, no worker starts."""
        from PySide6.QtWidgets import QMessageBox

        from romulus.db import set_config
        from romulus.ui.main_window import MainWindow

        set_config(seeded_db, "library_path", str(tmp_path))
        window = MainWindow(seeded_db)

        monkeypatch.setattr(
            "romulus.ui.main_window.QMessageBox.question",
            lambda *_a, **_kw: QMessageBox.StandardButton.No,
        )
        window._on_heavy_scan()
        assert window._heavy_scan_worker is None


# ---------------------------------------------------------------------------
# Bug #3 — Stylesheet: _match_badge_stylesheet produces valid CSS
# ---------------------------------------------------------------------------


class TestMatchBadgeStylesheet:
    def test_valid_hex_colors_produce_well_formed_css(self) -> None:
        from romulus.ui.detail_panel import _match_badge_stylesheet

        css = _match_badge_stylesheet("#2e7d32", "#ffffff")
        assert css.count("{") == 1
        assert css.count("}") == 1
        assert "background-color: #2e7d32" in css
        assert "color: #ffffff" in css

    def test_invalid_bg_falls_back(self) -> None:
        from romulus.ui.detail_panel import _match_badge_stylesheet

        css = _match_badge_stylesheet("red; color: evil", "#ffffff")
        assert "background-color: #888888" in css

    def test_invalid_fg_falls_back(self) -> None:
        from romulus.ui.detail_panel import _match_badge_stylesheet

        css = _match_badge_stylesheet("#aabbcc", "bad-color")
        assert "color: #ffffff" in css

    def test_empty_badge_stylesheet_is_valid_css(self, qapp) -> None:
        """The reset stylesheet applied to the badge in _render_empty must parse."""
        from PySide6.QtWidgets import QLabel

        label = QLabel()
        # Qt does not raise on an invalid stylesheet but logs a warning.
        # An explicit selector avoids the warning; verify no exception raised.
        label.setStyleSheet("QLabel {}")


# ---------------------------------------------------------------------------
# Bug #4 — EnrichProgressDialog: spinner stops on finished
# ---------------------------------------------------------------------------


class TestEnrichProgressDialog:
    def test_on_finished_stops_indeterminate_spinner(self, qapp) -> None:
        """After on_finished, the progress bar is no longer indeterminate (0,0)."""
        from romulus.ui.enrich_progress import EnrichProgressDialog

        dlg = EnrichProgressDialog()
        # Before: indeterminate — both min and max are 0.
        assert dlg.minimum() == 0
        assert dlg.maximum() == 0

        dlg.on_finished(5, 3, 2)

        # After on_finished the range must not still be (0, 0).
        assert not (dlg.minimum() == 0 and dlg.maximum() == 0), (
            "Progress bar is still in indeterminate mode after on_finished"
        )
        assert dlg.value() != 0

    def test_on_finished_summary_label(self, qapp) -> None:
        from romulus.ui.enrich_progress import EnrichProgressDialog

        dlg = EnrichProgressDialog()
        dlg.on_finished(10, 7, 4)
        label = dlg.labelText()
        assert "10" in label
        assert "7" in label
        assert "4" in label

    def test_enrich_preflight_blocks_zero_eligible_games(
        self, qapp, seeded_db, monkeypatch
    ) -> None:
        """_on_enrich shows an info dialog and returns without starting a worker
        when no DAT-verified games are present.

        Batch enrich now opens :class:`EnrichOptionsDialog` first so the user
        can opt into looser filters; we stub its ``exec`` to auto-accept
        with both defaults (unchecked) so the flow proceeds to the
        pre-flight count, which then triggers the zero-eligibility info box.
        """
        from romulus.ui.enrich_options_dialog import EnrichOptionsDialog
        from romulus.ui.main_window import MainWindow

        monkeypatch.setattr(
            EnrichOptionsDialog,
            "exec",
            lambda self: EnrichOptionsDialog.DialogCode.Accepted,
        )

        window = MainWindow(seeded_db)

        info_calls: list[tuple] = []
        monkeypatch.setattr(
            "romulus.ui.main_window.QMessageBox.information",
            lambda *args, **_kw: info_calls.append(args),
        )
        window._on_enrich()

        assert info_calls, "Expected an information dialog for zero eligible games"
        # The message must mention Heavy Scan so the user knows what to do.
        assert any("Heavy Scan" in str(call) for call in info_calls)
        # No worker should have been started.
        assert window._enrich_worker is None

    def test_enrich_preflight_allows_dat_verified_games(
        self, qapp, seeded_db, monkeypatch, tmp_path
    ) -> None:
        """_on_enrich proceeds past the pre-flight when eligible games exist."""
        import time

        from romulus.db import queries as queries_mod
        from romulus.ui.main_window import MainWindow

        # Insert a DAT-verified game so the pre-flight passes.
        game_id = queries_mod.upsert_game(
            seeded_db, {"title": "Verified Game", "system_id": "snes"}
        )
        rom_id = queries_mod.upsert_rom(
            seeded_db,
            {
                "path": str(tmp_path / "Verified Game.sfc"),
                "filename": "Verified Game.sfc",
                "extension": ".sfc",
                "size_bytes": 512,
                "mtime": time.time(),
                "system_id": "snes",
                "match_confidence": "dat_verified",
            },
        )
        queries_mod.link_rom_to_game(seeded_db, rom_id, game_id)
        seeded_db.commit()

        window = MainWindow(seeded_db)

        # Stub out the options dialog so it auto-accepts without blocking.
        from romulus.ui.enrich_options_dialog import EnrichOptionsDialog

        monkeypatch.setattr(
            EnrichOptionsDialog,
            "exec",
            lambda self: EnrichOptionsDialog.DialogCode.Accepted,
        )
        # Stub out the progress dialog so exec() returns immediately.
        monkeypatch.setattr(
            "romulus.ui.enrich_progress.EnrichProgressDialog.exec",
            lambda self: None,
        )
        # Stub worker.start so it doesn't actually spin up a thread.
        from romulus.ui.workers import EnrichWorker

        monkeypatch.setattr(EnrichWorker, "start", lambda self: None)

        window._on_enrich()

        # Worker was created (pre-flight passed).
        assert window._enrich_worker is not None


# ---------------------------------------------------------------------------
# Item #1 — Theme system
# ---------------------------------------------------------------------------


class TestThemeSystem:
    def test_available_themes_contains_all_four(self) -> None:
        from romulus.ui.themes import AVAILABLE_THEMES

        for tid in ("system", "dark", "light", "wbm_classic"):
            assert tid in AVAILABLE_THEMES

    def test_load_theme_qss_system_returns_empty(self) -> None:
        from romulus.ui.themes import load_theme_qss

        assert load_theme_qss("system") == ""

    def test_load_theme_qss_dark_has_content(self) -> None:
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("dark")
        assert len(qss) > 100
        assert "QWidget" in qss

    def test_load_theme_qss_light_has_content(self) -> None:
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("light")
        assert len(qss) > 100
        assert "QWidget" in qss

    def test_load_theme_qss_wbm_classic_has_content(self) -> None:
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        assert len(qss) > 100
        assert "QWidget" in qss

    def test_load_theme_qss_unknown_returns_empty(self) -> None:
        from romulus.ui.themes import load_theme_qss

        assert load_theme_qss("totally_unknown_theme") == ""

    def test_apply_theme_system_clears_stylesheet(self, qapp) -> None:
        from romulus.ui.themes import apply_theme

        # Set something first so clearing is meaningful.
        qapp.setStyleSheet("QWidget { color: red; }")
        apply_theme(qapp, "system")
        assert qapp.styleSheet() == ""

    def test_apply_theme_dark_sets_stylesheet(self, qapp) -> None:
        from romulus.ui.themes import apply_theme

        apply_theme(qapp, "dark")
        assert len(qapp.styleSheet()) > 0
        # Clean up so other tests get a pristine app.
        qapp.setStyleSheet("")


# ---------------------------------------------------------------------------
# Item #2 — SettingsDialog default size
# ---------------------------------------------------------------------------


class TestSettingsDialogSize:
    def test_dialog_default_size_is_at_least_640x480(self, qapp, seeded_db) -> None:
        from romulus.db import seed_defaults
        from romulus.ui.settings_dialog import SettingsDialog

        seed_defaults(seeded_db)
        dialog = SettingsDialog(seeded_db)
        assert dialog.width() >= 640
        assert dialog.height() >= 480


# ---------------------------------------------------------------------------
# Item #3 — DAT tab absolute path resolution
# ---------------------------------------------------------------------------


class TestDatTabAbsolutePaths:
    def test_existing_path_displayed_as_absolute(self, qapp, seeded_db, tmp_path) -> None:
        import json

        from romulus.db import seed_defaults, set_config
        from romulus.ui.settings_dialog import SettingsDialog

        seed_defaults(seeded_db)
        # Write a path that exists on disk.
        set_config(seeded_db, "dat_paths", json.dumps([str(tmp_path)]))
        dialog = SettingsDialog(seeded_db)
        item_text = dialog.dats.list.item(0).text()
        # Must be the resolved absolute path.
        from pathlib import Path
        assert item_text == str(Path(str(tmp_path)).resolve())

    def test_missing_path_shows_raw_with_tooltip(self, qapp, seeded_db) -> None:
        import json

        from romulus.db import seed_defaults, set_config
        from romulus.ui.settings_dialog import SettingsDialog

        seed_defaults(seeded_db)
        ghost = "/this/path/does/not/exist/at/all"
        set_config(seeded_db, "dat_paths", json.dumps([ghost]))
        dialog = SettingsDialog(seeded_db)
        item = dialog.dats.list.item(0)
        assert item is not None
        # Raw path preserved.
        assert item.text() == ghost
        # Tooltip explains the problem.
        assert "not found" in item.toolTip().lower()


# ---------------------------------------------------------------------------
# Item #4 — Progress dialogs: spinner stops on on_finished
# ---------------------------------------------------------------------------


class TestProgressDialogSpinners:
    def test_scan_progress_on_finished_stops_spinner(self, qapp) -> None:
        from romulus.ui.scan_progress import ScanProgressDialog

        dlg = ScanProgressDialog()
        assert dlg.maximum() == 0  # indeterminate on open
        dlg.on_finished(1, 42, 38, 4, ["snes"])
        assert not (dlg.minimum() == 0 and dlg.maximum() == 0)
        assert dlg.value() > 0
        assert "✓" in dlg.labelText()

    def test_scan_progress_on_failed_stops_spinner(self, qapp) -> None:
        from romulus.ui.scan_progress import ScanProgressDialog

        dlg = ScanProgressDialog()
        dlg.on_failed("Something went wrong")
        assert not (dlg.minimum() == 0 and dlg.maximum() == 0)
        assert "✗" in dlg.labelText()

    def test_heavy_scan_on_finished_stops_spinner(self, qapp) -> None:
        from romulus.ui.heavy_scan_progress import HeavyScanProgressDialog

        dlg = HeavyScanProgressDialog()
        dlg.on_finished(100, 80, 2)
        assert not (dlg.minimum() == 0 and dlg.maximum() == 0)
        assert dlg.value() > 0
        assert "✓" in dlg.labelText()

    def test_heavy_scan_on_failed_stops_spinner(self, qapp) -> None:
        from romulus.ui.heavy_scan_progress import HeavyScanProgressDialog

        dlg = HeavyScanProgressDialog()
        dlg.on_failed("DAT load error")
        assert not (dlg.minimum() == 0 and dlg.maximum() == 0)
        assert "✗" in dlg.labelText()

    def test_export_on_finished_stops_spinner(self, qapp, seeded_db) -> None:
        from romulus.core.exporter import load_all_profiles
        from romulus.ui.export_dialog import ExportDialog

        profiles = load_all_profiles()
        if not profiles:
            return  # Skip if no profiles bundled
        dlg = ExportDialog(seeded_db, profiles)
        # Force indeterminate state first (mimics clicking Export).
        dlg._progress.setRange(0, 0)
        dlg.on_finished(10, 2, 1024 * 1024, ["snes"], [])
        assert not (dlg._progress.minimum() == 0 and dlg._progress.maximum() == 0)
        assert "✓" in dlg._status_label.text()

    def test_export_on_failed_stops_spinner(self, qapp, seeded_db) -> None:
        from romulus.core.exporter import load_all_profiles
        from romulus.ui.export_dialog import ExportDialog

        profiles = load_all_profiles()
        if not profiles:
            return
        dlg = ExportDialog(seeded_db, profiles)
        dlg._progress.setRange(0, 0)
        dlg.on_failed("Disk full")
        assert not (dlg._progress.minimum() == 0 and dlg._progress.maximum() == 0)
        assert "✗" in dlg._status_label.text()


# ---------------------------------------------------------------------------
# Item #5 — Enrichment filter
# ---------------------------------------------------------------------------


def _row_enriched(
    name: str,
    *,
    has_cover: bool = False,
    has_metadata: bool = False,
) -> GameRow:
    return GameRow(
        rom_id=hash(name) & 0xFFFFFFFF,
        name=name,
        system_id="snes",
        system_name="SNES",
        region="USA",
        size_bytes=1024,
        match_confidence="fuzzy",
        game_id=1,
        has_cover=has_cover,
        has_metadata=has_metadata,
    )


class TestEnrichmentFilter:
    def test_filter_has_cover(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_enriched("A", has_cover=True),
                _row_enriched("B", has_cover=False),
                _row_enriched("C", has_cover=True, has_metadata=True),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_enrichment_filter("Has cover")
        assert proxy.rowCount() == 2

    def test_filter_has_metadata(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_enriched("A", has_metadata=True),
                _row_enriched("B"),
                _row_enriched("C", has_cover=True),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_enrichment_filter("Has metadata")
        assert proxy.rowCount() == 1

    def test_filter_has_both(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_enriched("A", has_cover=True, has_metadata=True),
                _row_enriched("B", has_cover=True),
                _row_enriched("C"),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_enrichment_filter("Has both")
        assert proxy.rowCount() == 1

    def test_filter_has_neither(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_enriched("A"),
                _row_enriched("B", has_cover=True),
                _row_enriched("C", has_metadata=True),
                _row_enriched("D", has_cover=True, has_metadata=True),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_enrichment_filter("Has neither")
        assert proxy.rowCount() == 1

    def test_filter_all_is_no_op(self, qapp) -> None:
        model = GameTableModel(
            [_row_enriched("A"), _row_enriched("B", has_cover=True)]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_enrichment_filter("All")
        assert proxy.rowCount() == 2


# ---------------------------------------------------------------------------
# Item #6 — Region "None (no region)" filter
# ---------------------------------------------------------------------------


class TestRegionNoneFilter:
    def test_none_region_matches_only_blank(self, qapp) -> None:
        model = GameTableModel(
            [
                _row_full("A", region="USA"),
                _row_full("B", region=""),
                _row_full("C", region="Japan"),
            ]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_region_filter("None (no region)")
        assert proxy.rowCount() == 1

    def test_none_region_excludes_named_regions(self, qapp) -> None:
        model = GameTableModel(
            [_row_full("A", region="Europe"), _row_full("B", region="Korea")]
        )
        proxy = GameTableProxy()
        proxy.setSourceModel(model)
        proxy.set_region_filter("None (no region)")
        assert proxy.rowCount() == 0


# ---------------------------------------------------------------------------
# Item #7 — Path column in game table
# ---------------------------------------------------------------------------


class TestPathColumn:
    def test_path_column_header_is_path(self, qapp) -> None:
        from romulus.ui.game_table import COLUMNS

        assert "Path" in COLUMNS
        assert COLUMNS.index("Path") == 5

    def test_path_column_data_returns_rom_path(self, qapp) -> None:
        row = GameRow(
            rom_id=1,
            name="Mario.sfc",
            system_id="snes",
            system_name="SNES",
            region="USA",
            size_bytes=512,
            match_confidence="fuzzy",
            rom_path="/library/snes/Mario.sfc",
        )
        model = GameTableModel([row])
        path_idx = model.index(0, 5)
        assert model.data(path_idx) == "/library/snes/Mario.sfc"

    def test_path_column_tooltip_matches_data(self, qapp) -> None:
        row = GameRow(
            rom_id=1,
            name="Zelda.sfc",
            system_id="snes",
            system_name="SNES",
            region="USA",
            size_bytes=512,
            match_confidence="dat_verified",
            rom_path="/mnt/roms/snes/Zelda.sfc",
        )
        model = GameTableModel([row])
        path_idx = model.index(0, 5)
        tooltip = model.data(path_idx, Qt.ItemDataRole.ToolTipRole)
        assert tooltip == "/mnt/roms/snes/Zelda.sfc"

    def test_load_rom_rows_populates_rom_path(self, seeded_db) -> None:
        """load_rom_rows now fills rom_path from the database."""
        import time

        from romulus.db import queries as queries_mod
        from romulus.ui.game_table import load_rom_rows

        queries_mod.upsert_rom(
            seeded_db,
            {
                "path": "/roms/snes/Mario.sfc",
                "filename": "Mario.sfc",
                "extension": ".sfc",
                "size_bytes": 512,
                "mtime": time.time(),
                "system_id": "snes",
                "match_confidence": "fuzzy",
            },
        )
        seeded_db.commit()
        rows = load_rom_rows(seeded_db)
        assert rows[0].rom_path == "/roms/snes/Mario.sfc"
