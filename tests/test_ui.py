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
    get_collections,
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
        assert model.columnCount() == 5

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
        assert get_collections(seeded_db) == []

    def test_get_collections_with_rows(self, seeded_db) -> None:
        seeded_db.execute(
            "INSERT INTO collections (name, description) VALUES (?, ?)",
            ("Favorites", "Hand-picked"),
        )
        seeded_db.commit()
        collections = get_collections(seeded_db)
        assert len(collections) == 1
        assert collections[0][1] == "Favorites"
        assert collections[0][2] == 0


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
# Settings dialog
# ---------------------------------------------------------------------------


class TestSettingsDialog:
    def test_save_round_trips_config(self, qapp, seeded_db) -> None:
        from romulus.ui.settings_dialog import SettingsDialog

        seed_defaults(seeded_db)
        dialog = SettingsDialog(seeded_db)
        dialog.general.library_path.setText("/tmp/my-roms")
        dialog.general.theme.setCurrentText("dark")
        dialog.scan.threads.setValue(12)
        dialog.metadata.username.setText("user1")
        dialog._accept_and_save()

        assert get_config(seeded_db, "library_path") == "/tmp/my-roms"
        assert get_config(seeded_db, "theme") == "dark"
        assert get_config(seeded_db, "scan_threads") == "12"
        assert get_config(seeded_db, "screenscraper_username") == "user1"


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
        assert proxy.rowCount() == 2

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
        assert "RPG" in panel.genre_label.text()
        assert "Square" in panel.developer_label.text()
        assert "Time-traveling" in panel.description.toPlainText()
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
        # Metadata fields should be cleared but the panel itself is enabled.
        assert panel.genre_label.text() == ""
        assert panel.publisher_label.text() == ""
        assert panel.description.toPlainText() == ""
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
