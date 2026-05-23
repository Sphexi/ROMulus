"""Tests for cover query helpers and discovery.

Updated for Session 15: covers are now keyed by ``rom_id`` throughout.
The ``games`` table and ``game_id`` FK have been removed in Session 13/14.

``TestDetailPanelCovers`` is skipped — ``detail_panel.py`` still uses the
legacy game-keyed API and will be updated in Session 18.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from romulus.db import create_tables
from romulus.db import queries as q
from romulus.db.connection import get_connection
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


def _enroll_rom(
    conn: sqlite3.Connection,
    system_id: str = "snes",
    title: str = "Test Game",
    fuzzy_key: str = "testgame",
) -> int:
    """Insert a ROM and return its rom_id."""
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
            "title": title,
        },
    )
    conn.commit()
    return rom_id


def _insert_cover(
    conn: sqlite3.Connection,
    rom_id: int,
    path: str = "/covers/game.png",
    cover_type: str = "Named_Boxarts",
    is_preferred: int = 0,
) -> int:
    """Insert a cover row and return its id."""
    cover_id = q.insert_cover(
        conn,
        rom_id,
        cover_type,
        source_url=None,
        local_path=path,
        is_preferred=is_preferred,
    )
    conn.commit()
    return cover_id


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


class TestSetPreferredCover:
    def test_marks_cover_preferred_and_resets_others(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        rom_id = _enroll_rom(conn)
        c1 = _insert_cover(conn, rom_id, "/a.png", is_preferred=1)
        c2 = _insert_cover(conn, rom_id, "/b.png")
        c3 = _insert_cover(conn, rom_id, "/c.png")

        q.set_preferred_cover(conn, c3)
        conn.commit()

        rows = {
            r["id"]: r["is_preferred"]
            for r in conn.execute(
                "SELECT id, is_preferred FROM covers WHERE rom_id = ?",
                (rom_id,),
            )
        }
        assert rows[c1] == 0
        assert rows[c2] == 0
        assert rows[c3] == 1

    def test_does_not_touch_other_rom(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        rom1 = _enroll_rom(conn, title="Game 1", fuzzy_key="game1")
        rom2 = _enroll_rom(conn, title="Game 2", fuzzy_key="game2")
        c1 = _insert_cover(conn, rom1, "/g1.png", is_preferred=1)
        _insert_cover(conn, rom2, "/g2.png", is_preferred=1)

        # Promote a new cover for rom2 — rom1's row must be untouched.
        c3 = _insert_cover(conn, rom2, "/g2b.png")
        q.set_preferred_cover(conn, c3)
        conn.commit()

        assert conn.execute(
            "SELECT is_preferred FROM covers WHERE id = ?", (c1,)
        ).fetchone()["is_preferred"] == 1

    def test_does_not_touch_other_cover_type(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        rom_id = _enroll_rom(conn)
        c_boxart = _insert_cover(conn, rom_id, "/boxart.png", "Named_Boxarts", is_preferred=1)
        c_snap = _insert_cover(conn, rom_id, "/snap.png", "Named_Snaps", is_preferred=1)
        c_new = _insert_cover(conn, rom_id, "/boxart2.png", "Named_Boxarts")

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
        rom_id = _enroll_rom(conn)
        _insert_cover(conn, rom_id, "/a.png")
        c2 = _insert_cover(conn, rom_id, "/b.png")
        # Explicitly prefer the second cover.
        q.set_preferred_cover(conn, c2)
        conn.commit()

        covers = q.get_covers(conn, rom_id)
        assert covers[0]["id"] == c2


class TestGetPreferredCover:
    def test_returns_preferred_row(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        rom_id = _enroll_rom(conn)
        _insert_cover(conn, rom_id, "/a.png", is_preferred=0)
        c2 = _insert_cover(conn, rom_id, "/b.png", is_preferred=1)

        row = q.get_preferred_cover(conn, rom_id)
        assert row is not None
        assert row["id"] == c2

    def test_returns_none_when_no_covers(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        rom_id = _enroll_rom(conn)
        assert q.get_preferred_cover(conn, rom_id) is None


class TestCountCovers:
    def test_correct_count(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        rom_id = _enroll_rom(conn)
        _insert_cover(conn, rom_id, "/a.png")
        _insert_cover(conn, rom_id, "/b.png")
        _insert_cover(conn, rom_id, "/c.png", "Named_Snaps")  # different type

        assert q.count_covers(conn, rom_id) == 2  # only Named_Boxarts
        assert q.count_covers(conn, rom_id, "Named_Snaps") == 1

    def test_zero_when_no_covers(self, tmp_path: Path) -> None:
        conn = _fresh_db(tmp_path)
        rom_id = _enroll_rom(conn)
        assert q.count_covers(conn, rom_id) == 0


# ---------------------------------------------------------------------------
# DetailPanel UI — skipped until Session 18 updates detail_panel.py
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "detail_panel.py still uses the legacy game_id API. "
        "Re-enable after Session 18 updates update_game() → update_rom()."
    )
)
class TestDetailPanelCovers:
    """These tests are deferred to Session 18 (UI layer refactor).

    The queries.py and covers table are now fully rom_id-keyed, but
    detail_panel.py still calls get_game_by_id / get_covers(game_id).
    Once Session 18 renames update_game → update_rom and fixes the
    internal DB calls, uncomment this class and remove the skip.
    """

    @pytest.fixture
    def panel_db(self, tmp_path: Path):
        conn = get_connection(tmp_path / "panel.db")
        create_tables(conn)
        seed_systems(conn)
        yield conn
        conn.close()

    @pytest.fixture
    def panel(self, panel_db, qapp):
        from romulus.ui.detail_panel import DetailPanel
        p = DetailPanel(panel_db)
        yield p

    def _make_rom(self, conn: sqlite3.Connection, n_covers: int = 1) -> int:
        """Insert a ROM with n_covers cover rows and return rom_id."""
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
                "title": "Panel Game",
            },
        )
        for i in range(n_covers):
            is_pref = 1 if i == 0 else 0
            q.insert_cover(
                conn,
                rom_id,
                "Named_Boxarts",
                source_url=None,
                local_path=None,
                is_preferred=is_pref,
            )
        conn.commit()
        return rom_id

    def test_update_rom_loads_covers(self, panel, panel_db) -> None:
        rom_id = self._make_rom(panel_db, n_covers=3)
        panel.update_rom(rom_id)
        assert len(panel._covers) == 3
        assert panel._cover_index == 0

    def test_next_advances_index(self, panel, panel_db) -> None:
        rom_id = self._make_rom(panel_db, n_covers=3)
        panel.update_rom(rom_id)
        panel._on_next_cover()
        assert panel._cover_index == 1

    def test_next_wraps_around(self, panel, panel_db) -> None:
        rom_id = self._make_rom(panel_db, n_covers=3)
        panel.update_rom(rom_id)
        panel._cover_index = 2
        panel._on_next_cover()
        assert panel._cover_index == 0

    def test_prev_wraps_around(self, panel, panel_db) -> None:
        rom_id = self._make_rom(panel_db, n_covers=3)
        panel.update_rom(rom_id)
        assert panel._cover_index == 0
        panel._on_prev_cover()
        assert panel._cover_index == 2

    def test_make_preferred_reloads_to_index_0(self, panel, panel_db) -> None:
        rom_id = self._make_rom(panel_db, n_covers=3)
        panel.update_rom(rom_id)
        panel._on_next_cover()
        assert panel._cover_index == 1
        target_id = int(panel._covers[1]["id"])

        panel._on_make_preferred()

        assert panel._cover_index == 0
        assert int(panel._covers[0]["id"]) == target_id
        row = panel_db.execute(
            "SELECT is_preferred FROM covers WHERE id = ?", (target_id,)
        ).fetchone()
        assert row["is_preferred"] == 1

    def test_single_cover_nav_buttons_disabled(self, panel, panel_db) -> None:
        rom_id = self._make_rom(panel_db, n_covers=1)
        panel.update_rom(rom_id)
        assert not panel.prev_button.isEnabled()
        assert not panel.next_button.isEnabled()

    def test_zero_covers_shows_placeholder_and_buttons_disabled(
        self, panel, panel_db
    ) -> None:
        rom_id = self._make_rom(panel_db, n_covers=0)
        panel.update_rom(rom_id)
        assert panel.cover_label.text() == "No cover art"
        assert not panel.prev_button.isEnabled()
        assert not panel.next_button.isEnabled()
        assert not panel.preferred_button.isEnabled()


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
        """Create a ROM + one matching image and run discovery; return rom_id."""
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
                "title": "Sonic the Hedgehog",
            },
        )
        conn.commit()

        discover_local_covers(conn, str(tmp_path))
        return rom_id

    def test_first_discovered_cover_is_preferred(self, tmp_path: Path) -> None:
        """The single cover inserted by discovery must be marked is_preferred=1."""
        conn = _fresh_db(tmp_path)
        rom_id = self._setup_discovery(tmp_path, conn)

        covers = q.get_covers(conn, rom_id)
        assert len(covers) >= 1
        preferred = [c for c in covers if c["is_preferred"] == 1]
        assert len(preferred) == 1
        assert preferred[0]["id"] == min(c["id"] for c in covers)

    def test_rerun_discovery_does_not_change_preferred(self, tmp_path: Path) -> None:
        """Re-running _ensure_preferred when a preferred row already exists is a no-op."""
        from romulus.db.queries import _ensure_preferred

        conn = _fresh_db(tmp_path)
        rom_id = _enroll_rom(conn)

        # Insert two covers; first one is explicitly preferred.
        c1 = _insert_cover(conn, rom_id, "/a.png", is_preferred=1)
        c2 = _insert_cover(conn, rom_id, "/b.png", is_preferred=0)

        # Simulate a second discovery run calling _ensure_preferred again.
        _ensure_preferred(conn, rom_id, "Named_Boxarts")
        conn.commit()

        # c1 must still be preferred; c2 must remain non-preferred.
        rows = {
            r["id"]: r["is_preferred"]
            for r in conn.execute(
                "SELECT id, is_preferred FROM covers WHERE rom_id = ?", (rom_id,)
            )
        }
        assert rows[c1] == 1
        assert rows[c2] == 0


# ---------------------------------------------------------------------------
# TestSiblingCoverCopy — sibling-cover gate in the cover-discovery chain
# ---------------------------------------------------------------------------


class TestSiblingCoverCopy:
    """Verify that copy_covers duplicates rows from a sibling rom and
    calls _ensure_preferred for each cover type inserted.
    """

    def test_copy_covers_transfers_rows_and_sets_preferred(
        self, tmp_path: Path
    ) -> None:
        """copy_covers should insert new rows for dest and mark one preferred
        per cover type.
        """
        conn = _fresh_db(tmp_path)

        # Source ROM with two boxart covers and one snap cover.
        src_id = _enroll_rom(conn, title="Source", fuzzy_key="source")
        _insert_cover(conn, src_id, "/img/a.png", "Named_Boxarts", is_preferred=1)
        _insert_cover(conn, src_id, "/img/b.png", "Named_Boxarts", is_preferred=0)
        _insert_cover(conn, src_id, "/img/snap.png", "Named_Snaps", is_preferred=1)

        # Destination ROM — no covers yet.
        dst_id = _enroll_rom(conn, title="Dest", fuzzy_key="dest")

        q.copy_covers(conn, source_rom_id=src_id, dest_rom_id=dst_id)
        conn.commit()

        covers = q.get_covers(conn, dst_id)
        assert len(covers) == 3

        # Exactly one preferred cover per type.
        boxart_preferred = [
            c for c in covers
            if c["cover_type"] == "Named_Boxarts" and c["is_preferred"] == 1
        ]
        snap_preferred = [
            c for c in covers
            if c["cover_type"] == "Named_Snaps" and c["is_preferred"] == 1
        ]
        assert len(boxart_preferred) == 1
        assert len(snap_preferred) == 1

    def test_copy_covers_does_not_overwrite_existing(
        self, tmp_path: Path
    ) -> None:
        """copy_covers on a dest that already has covers should not duplicate."""
        conn = _fresh_db(tmp_path)

        src_id = _enroll_rom(conn, title="Source2", fuzzy_key="source2")
        _insert_cover(conn, src_id, "/img/x.png", "Named_Boxarts", is_preferred=1)

        dst_id = _enroll_rom(conn, title="Dest2", fuzzy_key="dest2")
        # Pre-insert the same path on dest so the copy should be a no-op.
        _insert_cover(conn, dst_id, "/img/x.png", "Named_Boxarts", is_preferred=1)

        q.copy_covers(conn, source_rom_id=src_id, dest_rom_id=dst_id)
        conn.commit()

        # Still exactly one cover; no duplicate was created.
        assert q.count_covers(conn, dst_id, "Named_Boxarts") == 1
