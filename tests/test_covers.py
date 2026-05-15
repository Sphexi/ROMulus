"""Tests for 1:N cover support: schema migration, query helpers, UI, and discovery.

New tests (grouped by area):
  Schema  — 3 tests
  Queries — 5 tests
  UI      — 6 tests
  Discovery — 2 tests
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from romulus.db import create_tables
from romulus.db import queries as q
from romulus.db.connection import get_connection
from romulus.db.schema import _migrate_covers_add_is_preferred
from romulus.models import seed_systems

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    """Return a DB connection with schema + systems seeded."""
    conn = get_connection(tmp_path / "test.db")
    create_tables(conn)
    seed_systems(conn)
    return conn


def _enroll_game(
    conn: sqlite3.Connection,
    system_id: str = "snes",
    title: str = "Test Game",
    fuzzy_key: str = "testgame",
) -> tuple[int, int]:
    """Insert a ROM + game and return (rom_id, game_id)."""
    rom_id = q.upsert_rom(
        conn,
        {
            "path": f"/library/{system_id}/{title}.sfc",
            "filename": f"{title}.sfc",
            "extension": ".sfc",
            "size_bytes": 512,
            "mtime": time.time(),
            "system_id": system_id,
            "fuzzy_key": fuzzy_key,
        },
    )
    game_id = q.upsert_game(conn, {"title": title, "system_id": system_id})
    q.link_rom_to_game(conn, rom_id, game_id)
    conn.commit()
    return rom_id, game_id


def _insert_cover(
    conn: sqlite3.Connection,
    game_id: int,
    path: str = "/covers/game.png",
    cover_type: str = "Named_Boxarts",
    is_preferred: int = 0,
) -> int:
    """Insert a cover row and return its id."""
    cover_id = q.insert_cover(
        conn,
        game_id,
        cover_type,
        source_url=None,
        local_path=path,
        is_preferred=is_preferred,
    )
    conn.commit()
    return cover_id


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


class TestMigrationAddsIsPreferred:
    def test_column_added_on_existing_db(self, tmp_path: Path) -> None:
        """Simulate a pre-migration DB: create covers without is_preferred, migrate."""
        conn = get_connection(tmp_path / "old.db")
        # Create only the old covers table (no is_preferred).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS covers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id    INTEGER,
                cover_type TEXT NOT NULL,
                source_url TEXT,
                local_path TEXT,
                width      INTEGER,
                height     INTEGER
            )
            """
        )
        conn.commit()

        _migrate_covers_add_is_preferred(conn)

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(covers)")}
        assert "is_preferred" in cols
        conn.close()

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        """Calling migrate twice must not raise."""
        conn = get_connection(tmp_path / "idem.db")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS covers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id    INTEGER,
                cover_type TEXT NOT NULL,
                source_url TEXT,
                local_path TEXT,
                width      INTEGER,
                height     INTEGER
            )
            """
        )
        conn.commit()
        _migrate_covers_add_is_preferred(conn)
        # Second call must be a no-op (column already present).
        _migrate_covers_add_is_preferred(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(covers)")}
        assert "is_preferred" in cols
        conn.close()

    def test_migration_promotes_first_cover_per_group(self, tmp_path: Path) -> None:
        """After migration, the first cover per (game_id, cover_type) is preferred."""
        conn = get_connection(tmp_path / "promo.db")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS covers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id    INTEGER,
                cover_type TEXT NOT NULL,
                source_url TEXT,
                local_path TEXT,
                width      INTEGER,
                height     INTEGER
            )
            """
        )
        # Insert two covers for game 1 (same type) and one for game 2.
        sql = "INSERT INTO covers (game_id, cover_type, local_path) VALUES (?, ?, ?)"
        conn.execute(sql, (1, "Named_Boxarts", "/a.png"))
        conn.execute(sql, (1, "Named_Boxarts", "/b.png"))
        conn.execute(sql, (2, "Named_Boxarts", "/c.png"))
        conn.commit()

        _migrate_covers_add_is_preferred(conn)

        rows = conn.execute(
            "SELECT id, game_id, is_preferred FROM covers ORDER BY id"
        ).fetchall()
        # id=1 (first for game 1) → preferred; id=2 → not; id=3 (first for game 2) → preferred.
        assert rows[0]["is_preferred"] == 1
        assert rows[1]["is_preferred"] == 0
        assert rows[2]["is_preferred"] == 1
        conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


class TestSetPreferredCover:
    def test_marks_cover_preferred_and_resets_others(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)
        c1 = _insert_cover(conn, game_id, "/a.png", is_preferred=1)
        c2 = _insert_cover(conn, game_id, "/b.png")
        c3 = _insert_cover(conn, game_id, "/c.png")

        q.set_preferred_cover(conn, c3)
        conn.commit()

        rows = {
            r["id"]: r["is_preferred"]
            for r in conn.execute(
                "SELECT id, is_preferred FROM covers WHERE game_id = ?",
                (game_id,),
            )
        }
        assert rows[c1] == 0
        assert rows[c2] == 0
        assert rows[c3] == 1

    def test_does_not_touch_other_game(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game1 = _enroll_game(conn, title="Game 1", fuzzy_key="game1")
        _, game2 = _enroll_game(conn, title="Game 2", fuzzy_key="game2")
        c1 = _insert_cover(conn, game1, "/g1.png", is_preferred=1)
        _insert_cover(conn, game2, "/g2.png", is_preferred=1)

        # Promote a new cover for game2 — game1's row must be untouched.
        c3 = _insert_cover(conn, game2, "/g2b.png")
        q.set_preferred_cover(conn, c3)
        conn.commit()

        assert conn.execute(
            "SELECT is_preferred FROM covers WHERE id = ?", (c1,)
        ).fetchone()["is_preferred"] == 1

    def test_does_not_touch_other_cover_type(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)
        c_boxart = _insert_cover(conn, game_id, "/boxart.png", "Named_Boxarts", is_preferred=1)
        c_snap = _insert_cover(conn, game_id, "/snap.png", "Named_Snaps", is_preferred=1)
        c_new = _insert_cover(conn, game_id, "/boxart2.png", "Named_Boxarts")

        q.set_preferred_cover(conn, c_new)
        conn.commit()

        # Named_Snaps preferred row untouched.
        assert conn.execute(
            "SELECT is_preferred FROM covers WHERE id = ?", (c_snap,)
        ).fetchone()["is_preferred"] == 1
        # Old Named_Boxarts row reset.
        assert conn.execute(
            "SELECT is_preferred FROM covers WHERE id = ?", (c_boxart,)
        ).fetchone()["is_preferred"] == 0


class TestGetCoversOrdering:
    def test_preferred_row_sorts_first(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)
        _insert_cover(conn, game_id, "/a.png")
        c2 = _insert_cover(conn, game_id, "/b.png")
        # Explicitly prefer the second cover.
        q.set_preferred_cover(conn, c2)
        conn.commit()

        covers = q.get_covers(conn, game_id)
        assert covers[0]["id"] == c2


class TestGetPreferredCover:
    def test_returns_preferred_row(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)
        _insert_cover(conn, game_id, "/a.png", is_preferred=0)
        c2 = _insert_cover(conn, game_id, "/b.png", is_preferred=1)

        row = q.get_preferred_cover(conn, game_id)
        assert row is not None
        assert row["id"] == c2

    def test_returns_none_when_no_covers(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)
        assert q.get_preferred_cover(conn, game_id) is None


class TestCountCovers:
    def test_correct_count(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)
        _insert_cover(conn, game_id, "/a.png")
        _insert_cover(conn, game_id, "/b.png")
        _insert_cover(conn, game_id, "/c.png", "Named_Snaps")  # different type

        assert q.count_covers(conn, game_id) == 2  # only Named_Boxarts
        assert q.count_covers(conn, game_id, "Named_Snaps") == 1

    def test_zero_when_no_covers(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)
        assert q.count_covers(conn, game_id) == 0


# ---------------------------------------------------------------------------
# DetailPanel UI
# ---------------------------------------------------------------------------


@pytest.fixture
def panel_db(tmp_path: Path):
    """Yield a fresh DB for DetailPanel tests."""
    conn = get_connection(tmp_path / "panel.db")
    create_tables(conn)
    seed_systems(conn)
    yield conn
    conn.close()


@pytest.fixture
def panel(panel_db, qapp):
    """Yield a DetailPanel backed by panel_db."""
    from romulus.ui.detail_panel import DetailPanel

    p = DetailPanel(panel_db)
    yield p


def _make_game(conn: sqlite3.Connection, n_covers: int = 1) -> int:
    """Insert a game with n_covers cover rows and return game_id."""
    rom_id = q.upsert_rom(
        conn,
        {
            "path": "/library/snes/PanelGame.sfc",
            "filename": "PanelGame.sfc",
            "extension": ".sfc",
            "size_bytes": 512,
            "mtime": time.time(),
            "system_id": "snes",
            "fuzzy_key": "panelgame",
        },
    )
    game_id = q.upsert_game(conn, {"title": "Panel Game", "system_id": "snes"})
    q.link_rom_to_game(conn, rom_id, game_id)
    for i in range(n_covers):
        is_pref = 1 if i == 0 else 0
        q.insert_cover(
            conn,
            game_id,
            "Named_Boxarts",
            source_url=None,
            local_path=None,  # no real file; renders placeholder
            is_preferred=is_pref,
        )
    conn.commit()
    return game_id


class TestDetailPanelCovers:
    def test_update_game_loads_covers(self, panel, panel_db) -> None:
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        assert len(panel._covers) == 3
        assert panel._cover_index == 0

    def test_next_advances_index(self, panel, panel_db) -> None:
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        panel._on_next_cover()
        assert panel._cover_index == 1

    def test_next_wraps_around(self, panel, panel_db) -> None:
        """Next at the last cover wraps to index 0."""
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        panel._cover_index = 2
        panel._on_next_cover()
        assert panel._cover_index == 0

    def test_prev_wraps_around(self, panel, panel_db) -> None:
        """Prev at index 0 wraps to the last cover."""
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        assert panel._cover_index == 0
        panel._on_prev_cover()
        assert panel._cover_index == 2

    def test_make_preferred_reloads_to_index_0(self, panel, panel_db) -> None:
        """Star button calls set_preferred_cover and resets display to index 0."""
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        # Move to the second cover then star it.
        panel._on_next_cover()
        assert panel._cover_index == 1
        target_id = int(panel._covers[1]["id"])

        panel._on_make_preferred()

        # After make-preferred, index 0 should be the newly preferred cover.
        assert panel._cover_index == 0
        assert int(panel._covers[0]["id"]) == target_id
        # Confirm DB state.
        row = panel_db.execute(
            "SELECT is_preferred FROM covers WHERE id = ?", (target_id,)
        ).fetchone()
        assert row["is_preferred"] == 1

    def test_single_cover_nav_buttons_disabled(self, panel, panel_db) -> None:
        game_id = _make_game(panel_db, n_covers=1)
        panel.update_game(game_id)
        assert not panel.prev_button.isEnabled()
        assert not panel.next_button.isEnabled()

    def test_zero_covers_shows_placeholder_and_buttons_disabled(
        self, panel, panel_db
    ) -> None:
        game_id = _make_game(panel_db, n_covers=0)
        panel.update_game(game_id)
        assert panel.cover_label.text() == "No cover art"
        assert not panel.prev_button.isEnabled()
        assert not panel.next_button.isEnabled()
        assert not panel.preferred_button.isEnabled()

    def test_preferred_button_disabled_when_viewing_preferred_cover(
        self, panel, panel_db
    ) -> None:
        """User feedback: clicking Make Preferred had no visible effect.
        With 3 covers, index 0 is the preferred one — the button must
        show its filled-star state and be disabled.
        """
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        # Initial state: viewing index 0 = the preferred cover.
        assert not panel.preferred_button.isEnabled()
        assert panel.preferred_button.text() == "★ Preferred"
        # Index label shows the star marker for the preferred cover.
        assert panel.cover_index_label.text().startswith("★ 1 of 3")

    def test_preferred_button_enabled_when_viewing_non_preferred_cover(
        self, panel, panel_db
    ) -> None:
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        panel._on_next_cover()  # move to index 1 (not preferred)
        assert panel.preferred_button.isEnabled()
        assert panel.preferred_button.text() == "☆ Make preferred"
        # No star marker on non-preferred index.
        assert panel.cover_index_label.text() == "2 of 3"

    def test_clicking_preferred_updates_button_state(
        self, panel, panel_db
    ) -> None:
        """End-to-end: navigate to a non-preferred cover, click Make
        Preferred, and verify the button now reads "★ Preferred" — proving
        the state change actually persisted and the UI reflects it.
        """
        game_id = _make_game(panel_db, n_covers=3)
        panel.update_game(game_id)
        panel._on_next_cover()  # index 1, was not preferred
        cover_id_before = int(panel._covers[panel._cover_index]["id"])
        assert panel.preferred_button.text() == "☆ Make preferred"
        panel._on_make_preferred()
        # The just-preferred cover is now sorted to index 0.
        assert int(panel._covers[0]["id"]) == cover_id_before
        # Button state reflects: "I'm viewing the preferred cover now."
        assert not panel.preferred_button.isEnabled()
        assert panel.preferred_button.text() == "★ Preferred"
        # And it's persisted in the DB.
        row = panel_db.execute(
            "SELECT is_preferred FROM covers WHERE id = ?", (cover_id_before,)
        ).fetchone()
        assert row["is_preferred"] == 1


# ---------------------------------------------------------------------------
# Discovery — _ensure_preferred integration
# ---------------------------------------------------------------------------


class TestDiscoveryPreferred:
    """Test that _ensure_preferred is called correctly after cover insertion.

    Full filesystem discovery is used for the first-cover test.
    The idempotency test drives _ensure_preferred directly to avoid
    reliance on filename-matching nuances across image variants.
    """

    def _setup_discovery(self, tmp_path: Path, conn: sqlite3.Connection) -> int:
        """Create a ROM + one matching image and run discovery; return game_id."""
        from romulus.core.local_cover_finder import discover_local_covers

        system_dir = tmp_path / "snes"
        system_dir.mkdir(exist_ok=True)
        boxart_dir = system_dir / "boxart"
        boxart_dir.mkdir(exist_ok=True)

        rom_path = str(system_dir / "Sonic the Hedgehog.sfc")
        (system_dir / "Sonic the Hedgehog.sfc").write_bytes(b"\x00" * 16)
        # Single image whose fuzzy key matches the ROM's fuzzy key exactly.
        (boxart_dir / "Sonic the Hedgehog.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        )

        rom_id = q.upsert_rom(
            conn,
            {
                "path": rom_path,
                "filename": "Sonic the Hedgehog.sfc",
                "extension": ".sfc",
                "size_bytes": 16,
                "mtime": time.time(),
                "system_id": "snes",
                "fuzzy_key": "sonicthehedgehog",
            },
        )
        game_id = q.upsert_game(
            conn, {"title": "Sonic the Hedgehog", "system_id": "snes"}
        )
        q.link_rom_to_game(conn, rom_id, game_id)
        conn.commit()

        discover_local_covers(conn, str(tmp_path))
        return game_id

    def test_first_discovered_cover_is_preferred(self, tmp_path: Path) -> None:
        """The single cover inserted by discovery must be marked is_preferred=1."""
        conn = _fresh_db(tmp_path)
        game_id = self._setup_discovery(tmp_path, conn)

        covers = q.get_covers(conn, game_id)
        assert len(covers) >= 1
        preferred = [c for c in covers if c["is_preferred"] == 1]
        assert len(preferred) == 1
        assert preferred[0]["id"] == min(c["id"] for c in covers)

    def test_rerun_discovery_does_not_change_preferred(self, tmp_path: Path) -> None:
        """Re-running _ensure_preferred when a preferred row already exists is a no-op."""
        from romulus.db.queries import _ensure_preferred

        conn = _fresh_db(tmp_path)
        _, game_id = _enroll_game(conn)

        # Insert two covers; first one is explicitly preferred.
        c1 = _insert_cover(conn, game_id, "/a.png", is_preferred=1)
        c2 = _insert_cover(conn, game_id, "/b.png", is_preferred=0)

        # Simulate a second discovery run calling _ensure_preferred again.
        _ensure_preferred(conn, game_id, "Named_Boxarts")
        conn.commit()

        # c1 must still be preferred; c2 must remain non-preferred.
        rows = {
            r["id"]: r["is_preferred"]
            for r in conn.execute(
                "SELECT id, is_preferred FROM covers WHERE game_id = ?", (game_id,)
            )
        }
        assert rows[c1] == 1
        assert rows[c2] == 0
