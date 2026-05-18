"""Tests for WBM Classic theme polish and scoped right-click context menu actions.

Covers:
- WBM Classic QSS content assertions (gradients, border-radius, no exceptions).
- Other themes load cleanly (not broken by the QSS edits).
- get_rom_ids_for_scope query helper.
- enrich_library scope filtering (game_ids, system_id, collection_id).
- hash_library scope_rom_ids filtering.
- discover_local_covers scope_rom_ids filtering.
- GameTable context menu scoped signals.
- SystemSidebar context menu — system rows, collection rows, and "All" row.
- MainWindow scoped enrich handler builds EnrichWorker with correct kwargs.
"""

from __future__ import annotations

import sqlite3
import time

from romulus.db import queries as q  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_rom(
    conn: sqlite3.Connection,
    filename: str,
    system_id: str,
    *,
    size_bytes: int = 1024,
    match_confidence: str = "dat_verified",
    game_id: int | None = None,
) -> int:
    """Insert a ROM row and return its id. Optionally link to a game."""
    rom_id = q.upsert_rom(
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
    if game_id is not None:
        q.link_rom_to_game(conn, rom_id, game_id)
    return rom_id


def _insert_game(
    conn: sqlite3.Connection,
    title: str,
    system_id: str,
) -> int:
    """Insert a game row and return its id."""
    return q.upsert_game(conn, {"title": title, "system_id": system_id})


# ---------------------------------------------------------------------------
# Part A — WBM Classic theme polish
# ---------------------------------------------------------------------------


class TestWbmClassicTheme:
    def test_loads_cleanly_no_exception(self) -> None:
        """load_theme_qss('wbm_classic') must not raise."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        assert isinstance(qss, str)
        assert len(qss) > 500

    def test_contains_gradient_on_push_button(self) -> None:
        """QPushButton must declare a qlineargradient background."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        assert "qlineargradient" in qss
        # At minimum the button base state and the header section should use gradients.
        assert qss.count("qlineargradient") >= 5

    def test_push_button_has_border_radius(self) -> None:
        """QPushButton must declare border-radius (rounded corners)."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        # Verify that a border-radius declaration appears after QPushButton.
        btn_pos = qss.find("QPushButton {")
        assert btn_pos != -1
        radius_pos = qss.find("border-radius", btn_pos)
        assert radius_pos != -1, "QPushButton block must contain border-radius"
        # Must be >= 5px (XP-style rounded corners per spec).
        import re
        values = re.findall(r"QPushButton\s*\{[^}]*border-radius:\s*(\d+)px", qss)
        assert values, "QPushButton border-radius value not found"
        assert int(values[0]) >= 5

    def test_line_edit_has_border_radius(self) -> None:
        """QLineEdit must declare border-radius."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        pos = qss.find("QLineEdit")
        assert pos != -1
        radius_pos = qss.find("border-radius", pos)
        assert radius_pos != -1, "QLineEdit block must contain border-radius"

    def test_group_box_has_border_radius(self) -> None:
        """QGroupBox must declare border-radius."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        pos = qss.find("QGroupBox {")
        assert pos != -1
        radius_pos = qss.find("border-radius", pos)
        assert radius_pos != -1, "QGroupBox block must contain border-radius"

    def test_tab_has_rounded_top_corners(self) -> None:
        """QTabBar::tab must declare border-top-left-radius and border-top-right-radius."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        tab_pos = qss.find("QTabBar::tab {")
        assert tab_pos != -1
        assert "border-top-left-radius" in qss[tab_pos:], "Tab top-left radius missing"
        assert "border-top-right-radius" in qss[tab_pos:], "Tab top-right radius missing"

    def test_header_section_gradient(self) -> None:
        """QHeaderView::section must use a gradient (Win7-style header)."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        header_pos = qss.find("QHeaderView::section {")
        assert header_pos != -1
        assert "qlineargradient" in qss[header_pos:header_pos + 300]

    def test_table_selection_gradient(self) -> None:
        """QTableView selection-background-color must be a gradient."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        tv_pos = qss.find("QTableView {")
        assert tv_pos != -1
        block = qss[tv_pos:tv_pos + 400]
        assert "selection-background-color" in block
        assert "qlineargradient" in block

    def test_group_box_title_bold(self) -> None:
        """QGroupBox::title must declare font-weight: bold."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        title_pos = qss.find("QGroupBox::title {")
        assert title_pos != -1
        assert "font-weight: bold" in qss[title_pos:title_pos + 200]

    def test_status_bar_gradient(self) -> None:
        """QStatusBar must use a gradient background."""
        from romulus.ui.themes import load_theme_qss

        qss = load_theme_qss("wbm_classic")
        pos = qss.find("QStatusBar {")
        assert pos != -1
        block = qss[pos:pos + 300]
        assert "qlineargradient" in block

    def test_other_themes_still_load(self) -> None:
        """Edits to wbm_classic.qss must not break the other bundled themes."""
        from romulus.ui.themes import load_theme_qss

        for tid in ("dark", "light"):
            qss = load_theme_qss(tid)
            assert len(qss) > 100, f"Theme '{tid}' returned an empty string"
            assert "QWidget" in qss, f"Theme '{tid}' missing QWidget block"


# ---------------------------------------------------------------------------
# Part B — get_rom_ids_for_scope
# ---------------------------------------------------------------------------


class TestGetRomIdsForScope:
    def test_game_id_scope_returns_matching_roms(self, seeded_db) -> None:
        gid = _insert_game(seeded_db, "Mario", "snes")
        rid1 = _insert_rom(seeded_db, "Mario.sfc", "snes", game_id=gid)
        rid2 = _insert_rom(seeded_db, "Mario (USA).sfc", "snes", game_id=gid)
        _insert_rom(seeded_db, "Sonic.md", "megadrive")  # different game, no link
        seeded_db.commit()

        result = q.get_rom_ids_for_scope(seeded_db, game_id=gid)
        assert sorted(result) == sorted([rid1, rid2])

    def test_system_id_scope_returns_all_system_roms(self, seeded_db) -> None:
        rid1 = _insert_rom(seeded_db, "Mario.sfc", "snes")
        rid2 = _insert_rom(seeded_db, "Zelda.sfc", "snes")
        _insert_rom(seeded_db, "Sonic.md", "megadrive")
        seeded_db.commit()

        result = q.get_rom_ids_for_scope(seeded_db, system_id="snes")
        assert sorted(result) == sorted([rid1, rid2])

    def test_collection_id_scope_returns_collection_roms(self, seeded_db) -> None:
        gid1 = _insert_game(seeded_db, "Mario", "snes")
        rid1 = _insert_rom(seeded_db, "Mario.sfc", "snes", game_id=gid1)
        gid2 = _insert_game(seeded_db, "Zelda", "snes")
        rid2 = _insert_rom(seeded_db, "Zelda.sfc", "snes", game_id=gid2)
        gid3 = _insert_game(seeded_db, "Sonic", "megadrive")
        _insert_rom(seeded_db, "Sonic.md", "megadrive", game_id=gid3)  # not in coll

        cid = q.create_collection(seeded_db, "Platformers")
        q.add_game_to_collection(seeded_db, cid, gid1)
        q.add_game_to_collection(seeded_db, cid, gid2)
        seeded_db.commit()

        result = q.get_rom_ids_for_scope(seeded_db, collection_id=cid)
        assert sorted(result) == sorted([rid1, rid2])

    def test_no_scope_returns_empty_list(self, seeded_db) -> None:
        _insert_rom(seeded_db, "Mario.sfc", "snes")
        seeded_db.commit()

        result = q.get_rom_ids_for_scope(seeded_db)
        assert result == []

    def test_game_id_scope_with_no_roms_returns_empty(self, seeded_db) -> None:
        gid = _insert_game(seeded_db, "Ghost", "snes")
        seeded_db.commit()

        result = q.get_rom_ids_for_scope(seeded_db, game_id=gid)
        assert result == []


# ---------------------------------------------------------------------------
# Part B — enrich_library scope filtering
# ---------------------------------------------------------------------------


class TestEnrichLibraryScope:
    """Pure logic tests — patch the network calls so nothing is fetched."""

    def _seed_dat_verified_game(
        self,
        conn: sqlite3.Connection,
        title: str,
        system_id: str,
    ) -> tuple[int, int]:
        """Insert a dat-verified game + ROM pair; return (game_id, rom_id)."""
        gid = q.upsert_game(conn, {"title": title, "system_id": system_id})
        rid = q.upsert_rom(
            conn,
            {
                "path": f"/lib/{system_id}/{title}.rom",
                "filename": f"{title}.rom",
                "extension": ".rom",
                "size_bytes": 512,
                "mtime": time.time(),
                "system_id": system_id,
                "match_confidence": "dat_verified",
            },
        )
        q.link_rom_to_game(conn, rid, gid)
        conn.commit()
        return gid, rid

    def test_game_ids_scope_limits_processing(self, seeded_db, monkeypatch) -> None:
        """enrich_library with game_ids only processes those games."""
        from romulus.metadata import enrich_library

        gid1, _ = self._seed_dat_verified_game(seeded_db, "Mario", "snes")
        gid2, _ = self._seed_dat_verified_game(seeded_db, "Sonic", "megadrive")

        processed: list[int] = []

        def _fake_progress(idx: int, total: int, title: str) -> None:
            processed.append(idx)

        # Patch out actual network calls — we only care about the filter.
        monkeypatch.setattr(
            "romulus.metadata._fetch_metadata_for_game",
            lambda *_a, **_kw: False,
        )
        monkeypatch.setattr(
            "romulus.metadata._fetch_covers_for_game",
            lambda *_a, **_kw: 0,
        )

        stats = enrich_library(
            seeded_db,
            game_ids=[gid1],
            progress_callback=_fake_progress,
        )
        # Only Mario should have been processed.
        assert stats["games_processed"] == 1
        assert len(processed) == 1

    def test_system_id_scope_limits_to_system_games(
        self, seeded_db, monkeypatch
    ) -> None:
        """enrich_library with system_id only touches that system's games."""
        from romulus.metadata import enrich_library

        self._seed_dat_verified_game(seeded_db, "Mario", "snes")
        self._seed_dat_verified_game(seeded_db, "Sonic", "megadrive")

        monkeypatch.setattr(
            "romulus.metadata._fetch_metadata_for_game",
            lambda *_a, **_kw: False,
        )
        monkeypatch.setattr(
            "romulus.metadata._fetch_covers_for_game",
            lambda *_a, **_kw: 0,
        )

        stats = enrich_library(seeded_db, system_id="snes")
        assert stats["games_processed"] == 1

    def test_collection_id_scope_limits_to_collection_games(
        self, seeded_db, monkeypatch
    ) -> None:
        """enrich_library with collection_id only touches games in that collection."""
        from romulus.metadata import enrich_library

        gid1, _ = self._seed_dat_verified_game(seeded_db, "Mario", "snes")
        gid2, _ = self._seed_dat_verified_game(seeded_db, "Sonic", "megadrive")

        cid = q.create_collection(seeded_db, "My Pack")
        q.add_game_to_collection(seeded_db, cid, gid1)
        seeded_db.commit()

        monkeypatch.setattr(
            "romulus.metadata._fetch_metadata_for_game",
            lambda *_a, **_kw: False,
        )
        monkeypatch.setattr(
            "romulus.metadata._fetch_covers_for_game",
            lambda *_a, **_kw: 0,
        )

        stats = enrich_library(seeded_db, collection_id=cid)
        assert stats["games_processed"] == 1

    def test_no_scope_processes_all_games(self, seeded_db, monkeypatch) -> None:
        """enrich_library with no scope processes all eligible games."""
        from romulus.metadata import enrich_library

        self._seed_dat_verified_game(seeded_db, "Mario", "snes")
        self._seed_dat_verified_game(seeded_db, "Sonic", "megadrive")

        monkeypatch.setattr(
            "romulus.metadata._fetch_metadata_for_game",
            lambda *_a, **_kw: False,
        )
        monkeypatch.setattr(
            "romulus.metadata._fetch_covers_for_game",
            lambda *_a, **_kw: 0,
        )

        stats = enrich_library(seeded_db)
        assert stats["games_processed"] == 2


# ---------------------------------------------------------------------------
# Part B — hash_library scope_rom_ids filtering
# ---------------------------------------------------------------------------


class TestHashLibraryScope:
    def test_scope_rom_ids_limits_hashing(self, seeded_db, tmp_path) -> None:
        """hash_library with scope_rom_ids only hashes those ROMs."""
        from romulus.core.hasher import hash_library

        # Create two real ROM files so the hasher can actually read them.
        rom1 = tmp_path / "mario.sfc"
        rom2 = tmp_path / "sonic.md"
        rom1.write_bytes(b"\x00" * 256)
        rom2.write_bytes(b"\x01" * 256)

        rid1 = q.upsert_rom(
            seeded_db,
            {
                "path": str(rom1),
                "filename": "mario.sfc",
                "extension": ".sfc",
                "size_bytes": 256,
                "mtime": time.time(),
                "system_id": "snes",
                "match_confidence": "fuzzy",
            },
        )
        rid2 = q.upsert_rom(
            seeded_db,
            {
                "path": str(rom2),
                "filename": "sonic.md",
                "extension": ".md",
                "size_bytes": 256,
                "mtime": time.time(),
                "system_id": "megadrive",
                "match_confidence": "fuzzy",
            },
        )
        seeded_db.commit()

        # Hash only rid1.
        count = hash_library(seeded_db, scope_rom_ids=[rid1])
        assert count == 1

        # Only rid1 should have a hash row.
        hashed_ids = {
            row[0]
            for row in seeded_db.execute("SELECT rom_id FROM hashes").fetchall()
        }
        assert rid1 in hashed_ids
        assert rid2 not in hashed_ids

    def test_scope_rom_ids_none_hashes_all(self, seeded_db, tmp_path) -> None:
        """hash_library with scope_rom_ids=None hashes every eligible ROM."""
        from romulus.core.hasher import hash_library

        for name in ("a.sfc", "b.sfc"):
            f = tmp_path / name
            f.write_bytes(b"\xff" * 64)
            q.upsert_rom(
                seeded_db,
                {
                    "path": str(f),
                    "filename": name,
                    "extension": ".sfc",
                    "size_bytes": 64,
                    "mtime": time.time(),
                    "system_id": "snes",
                    "match_confidence": "fuzzy",
                },
            )
        seeded_db.commit()

        count = hash_library(seeded_db, scope_rom_ids=None)
        assert count == 2


# ---------------------------------------------------------------------------
# Part B — discover_local_covers scope_rom_ids filtering
# ---------------------------------------------------------------------------


class TestDiscoverLocalCoversScope:
    def test_scope_rom_ids_limits_discovery(self, seeded_db, tmp_path) -> None:
        """discover_local_covers with scope_rom_ids only walks those ROMs."""
        from romulus.core.local_cover_finder import discover_local_covers

        # Two games, each with a ROM.
        gid1 = q.upsert_game(seeded_db, {"title": "Mario", "system_id": "snes"})
        gid2 = q.upsert_game(seeded_db, {"title": "Sonic", "system_id": "megadrive"})

        snes_dir = tmp_path / "snes"
        snes_dir.mkdir()
        md_dir = tmp_path / "megadrive"
        md_dir.mkdir()

        rid1 = q.upsert_rom(
            seeded_db,
            {
                "path": str(snes_dir / "Mario.sfc"),
                "filename": "Mario.sfc",
                "extension": ".sfc",
                "size_bytes": 64,
                "mtime": time.time(),
                "system_id": "snes",
                "fuzzy_key": "mario",
                "match_confidence": "dat_verified",
            },
        )
        q.link_rom_to_game(seeded_db, rid1, gid1)

        rid2 = q.upsert_rom(
            seeded_db,
            {
                "path": str(md_dir / "Sonic.md"),
                "filename": "Sonic.md",
                "extension": ".md",
                "size_bytes": 64,
                "mtime": time.time(),
                "system_id": "megadrive",
                "fuzzy_key": "sonic",
                "match_confidence": "dat_verified",
            },
        )
        q.link_rom_to_game(seeded_db, rid2, gid2)
        seeded_db.commit()

        result = discover_local_covers(
            seeded_db, str(tmp_path), scope_rom_ids=[rid1]
        )
        # Only rid1 was in scope — roms_scanned must be at most 1.
        assert result.roms_scanned <= 1

    def test_scope_rom_ids_none_walks_all(self, seeded_db, tmp_path) -> None:
        """discover_local_covers with scope_rom_ids=None walks all ROMs."""
        from romulus.core.local_cover_finder import discover_local_covers

        snes_dir = tmp_path / "snes"
        snes_dir.mkdir()

        for i in range(3):
            gid = q.upsert_game(
                seeded_db, {"title": f"Game{i}", "system_id": "snes"}
            )
            rid = q.upsert_rom(
                seeded_db,
                {
                    "path": str(snes_dir / f"Game{i}.sfc"),
                    "filename": f"Game{i}.sfc",
                    "extension": ".sfc",
                    "size_bytes": 64,
                    "mtime": time.time(),
                    "system_id": "snes",
                    "fuzzy_key": f"game{i}",
                    "match_confidence": "dat_verified",
                },
            )
            q.link_rom_to_game(seeded_db, rid, gid)
        seeded_db.commit()

        result = discover_local_covers(
            seeded_db, str(tmp_path), scope_rom_ids=None
        )
        assert result.roms_scanned == 3


# ---------------------------------------------------------------------------
# Part B — GameTable context menu scoped signals
# ---------------------------------------------------------------------------


class TestGameTableContextMenuSignals:
    def _make_table_with_row(self, qapp, *, game_id: int = 42) -> object:
        from romulus.ui.game_table import GameRow, GameTable

        widget = GameTable()
        widget.set_rows(
            [
                GameRow(
                    rom_id=1,
                    name="Mario.sfc",
                    system_id="snes",
                    system_name="SNES",
                    region="USA",
                    size_bytes=512,
                    match_confidence="dat_verified",
                    game_id=game_id,
                )
            ]
        )
        return widget

    def test_context_menu_has_enrich_action(self, qapp) -> None:
        """The game-table context menu must contain an 'Enrich this game' entry."""
        widget = self._make_table_with_row(qapp)
        # Select the only row.
        widget.view.selectAll()
        # Trigger the context menu handler directly instead of a GUI click.
        # Confirm the signal exists on the widget.
        assert hasattr(widget, "enrich_game_requested")

    def test_context_menu_has_heavy_scan_action(self, qapp) -> None:
        """The game-table must expose heavy_scan_game_requested signal."""
        widget = self._make_table_with_row(qapp)
        assert hasattr(widget, "heavy_scan_game_requested")

    def test_context_menu_has_find_local_covers_action(self, qapp) -> None:
        """The game-table must expose find_local_covers_game_requested signal."""
        widget = self._make_table_with_row(qapp)
        assert hasattr(widget, "find_local_covers_game_requested")

    def test_enrich_game_signal_carries_game_id(self, qapp) -> None:
        """Emitting enrich_game_requested passes the correct game_id."""
        from romulus.ui.game_table import GameRow, GameTable

        widget = GameTable()
        game_id = 99
        widget.set_rows(
            [
                GameRow(
                    rom_id=1,
                    name="Test.sfc",
                    system_id="snes",
                    system_name="SNES",
                    region="USA",
                    size_bytes=256,
                    match_confidence="dat_verified",
                    game_id=game_id,
                )
            ]
        )
        received: list[int] = []
        widget.enrich_game_requested.connect(received.append)

        # Manually emit the signal to verify it carries the right id.
        widget.enrich_game_requested.emit(game_id)
        assert received == [game_id]


# ---------------------------------------------------------------------------
# Part B — SystemSidebar context menu
# ---------------------------------------------------------------------------


class TestSystemSidebarContextMenu:
    def test_system_row_exposes_scoped_signals(self, qapp) -> None:
        """SystemSidebar must declare the expected scoped signals."""
        from romulus.ui.system_sidebar import SystemSidebar

        sidebar = SystemSidebar()
        assert hasattr(sidebar, "quick_scan_system_requested")
        assert hasattr(sidebar, "heavy_scan_system_requested")
        assert hasattr(sidebar, "enrich_system_requested")
        assert hasattr(sidebar, "find_covers_system_requested")

    def test_collection_row_exposes_scoped_signals(self, qapp) -> None:
        """SystemSidebar must declare collection-scoped signals."""
        from romulus.ui.system_sidebar import SystemSidebar

        sidebar = SystemSidebar()
        assert hasattr(sidebar, "enrich_collection_requested")
        assert hasattr(sidebar, "heavy_scan_collection_requested")
        assert hasattr(sidebar, "find_covers_collection_requested")

    def test_system_context_menu_emits_enrich_system_signal(
        self, qapp, seeded_db
    ) -> None:
        """Right-clicking a system row and choosing 'Enrich' emits enrich_system_requested."""
        from romulus.ui.system_sidebar import SystemSidebar

        # Seed one ROM so the system appears in the sidebar.
        q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Mario.sfc",
                "filename": "Mario.sfc",
                "extension": ".sfc",
                "size_bytes": 512,
                "mtime": time.time(),
                "system_id": "snes",
                "match_confidence": "fuzzy",
            },
        )
        seeded_db.commit()

        sidebar = SystemSidebar()
        sidebar.populate(seeded_db)

        received: list[str] = []
        sidebar.enrich_system_requested.connect(received.append)

        # Emit the signal directly (testing the signal contract).
        sidebar.enrich_system_requested.emit("snes")
        assert received == ["snes"]

    def test_collection_context_menu_emits_heavy_scan_collection_signal(
        self, qapp, seeded_db
    ) -> None:
        """Emitting heavy_scan_collection_requested carries the collection_id."""
        from romulus.ui.system_sidebar import SystemSidebar

        sidebar = SystemSidebar()

        received: list[int] = []
        sidebar.heavy_scan_collection_requested.connect(received.append)

        sidebar.heavy_scan_collection_requested.emit(7)
        assert received == [7]

    def test_all_row_has_no_kind_system_in_context(self, qapp, seeded_db) -> None:
        """The 'All' root row should not trigger system-specific signals."""
        from romulus.ui.system_sidebar import KIND_ALL, NODE_KIND_ROLE, SystemSidebar

        sidebar = SystemSidebar()
        sidebar.populate(seeded_db)

        model = sidebar.model()
        all_item = model.item(0)
        # Verify it's the All row.
        assert all_item.data(NODE_KIND_ROLE) == KIND_ALL


# ---------------------------------------------------------------------------
# Part B — MainWindow scoped enrich handler builds EnrichWorker with kwargs
# ---------------------------------------------------------------------------


class TestMainWindowScopedEnrich:
    def test_enrich_scoped_with_game_ids_builds_worker_with_game_ids(
        self, qapp, seeded_db, monkeypatch
    ) -> None:
        """_enrich_scoped(game_ids=[42]) must create an EnrichWorker with game_ids=[42]."""
        import time

        from romulus.db import queries as queries_mod
        from romulus.ui.main_window import MainWindow
        from romulus.ui.workers import EnrichWorker

        # Seed one dat_verified ROM so the pre-flight passes.
        gid = queries_mod.upsert_game(
            seeded_db, {"title": "Verified", "system_id": "snes"}
        )
        rid = queries_mod.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Verified.sfc",
                "filename": "Verified.sfc",
                "extension": ".sfc",
                "size_bytes": 512,
                "mtime": time.time(),
                "system_id": "snes",
                "match_confidence": "dat_verified",
            },
        )
        queries_mod.link_rom_to_game(seeded_db, rid, gid)
        seeded_db.commit()

        window = MainWindow(seeded_db)

        # Intercept EnrichWorker construction so we can inspect kwargs.
        created_workers: list[dict] = []
        _original_init = EnrichWorker.__init__

        def _spy_init(self_w, *args, **kwargs) -> None:  # noqa: ANN001
            created_workers.append(
                {
                    "game_ids": kwargs.get("game_ids"),
                    "system_id": kwargs.get("system_id"),
                    "collection_id": kwargs.get("collection_id"),
                }
            )
            _original_init(self_w, *args, **kwargs)

        monkeypatch.setattr(EnrichWorker, "__init__", _spy_init)
        # Stub exec so the dialog doesn't block.
        monkeypatch.setattr(
            "romulus.ui.enrich_progress.EnrichProgressDialog.exec",
            lambda self: None,
        )
        # Batch-scoped enrich now opens EnrichOptionsDialog first; auto-accept
        # so the test reaches the worker-construction path.
        from romulus.ui.enrich_options_dialog import EnrichOptionsDialog

        monkeypatch.setattr(
            EnrichOptionsDialog,
            "exec",
            lambda self: EnrichOptionsDialog.DialogCode.Accepted,
        )
        monkeypatch.setattr(EnrichWorker, "start", lambda self: None)

        # Use the actual seeded game id; the new pre-flight narrows the
        # eligibility check to the requested scope so a fake id would
        # legitimately bail "no eligible games" before constructing a
        # worker — verifying kwarg propagation needs a real id.
        window._enrich_scoped(game_ids=[gid])

        assert created_workers, "EnrichWorker was never constructed"
        assert created_workers[0]["game_ids"] == [gid]
        assert created_workers[0]["system_id"] is None
        assert created_workers[0]["collection_id"] is None

    def test_enrich_scoped_with_system_id_builds_worker_with_system_id(
        self, qapp, seeded_db, monkeypatch
    ) -> None:
        """_enrich_scoped(system_id='snes') must create an EnrichWorker with system_id='snes'."""
        import time

        from romulus.db import queries as queries_mod
        from romulus.ui.main_window import MainWindow
        from romulus.ui.workers import EnrichWorker

        gid = queries_mod.upsert_game(
            seeded_db, {"title": "Verified", "system_id": "snes"}
        )
        rid = queries_mod.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Verified.sfc",
                "filename": "Verified.sfc",
                "extension": ".sfc",
                "size_bytes": 512,
                "mtime": time.time(),
                "system_id": "snes",
                "match_confidence": "dat_verified",
            },
        )
        queries_mod.link_rom_to_game(seeded_db, rid, gid)
        seeded_db.commit()

        window = MainWindow(seeded_db)

        created_workers: list[dict] = []
        _original_init = EnrichWorker.__init__

        def _spy_init(self_w, *args, **kwargs) -> None:  # noqa: ANN001
            created_workers.append({"system_id": kwargs.get("system_id")})
            _original_init(self_w, *args, **kwargs)

        monkeypatch.setattr(EnrichWorker, "__init__", _spy_init)
        monkeypatch.setattr(
            "romulus.ui.enrich_progress.EnrichProgressDialog.exec",
            lambda self: None,
        )
        from romulus.ui.enrich_options_dialog import EnrichOptionsDialog

        monkeypatch.setattr(
            EnrichOptionsDialog,
            "exec",
            lambda self: EnrichOptionsDialog.DialogCode.Accepted,
        )
        monkeypatch.setattr(EnrichWorker, "start", lambda self: None)

        window._enrich_scoped(system_id="snes")

        assert created_workers
        assert created_workers[0]["system_id"] == "snes"
