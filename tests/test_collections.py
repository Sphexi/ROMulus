"""Tests for the collections system — queries + favorite toggle behavior.

These tests exercise the SQL layer (queries.py) only. The UI side (the
DetailPanel ★ button, the right-click submenu) is covered in test_ui.py
where the qapp fixture is available.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from romulus.db import queries as q


def _add_game(conn: sqlite3.Connection, title: str, system_id: str = "snes") -> int:
    """Insert a minimal `games` row and return its id."""
    return q.upsert_game(conn, {"title": title, "system_id": system_id})


def _add_rom_for_game(
    conn: sqlite3.Connection,
    game_id: int,
    filename: str,
    system_id: str = "snes",
) -> int:
    """Insert a `roms` row already linked to a given game id."""
    rom_id = q.upsert_rom(
        conn,
        {
            "path": f"/library/{system_id}/{filename}",
            "filename": filename,
            "extension": "." + filename.rsplit(".", 1)[-1],
            "size_bytes": 1024,
            "mtime": time.time(),
            "system_id": system_id,
            "fuzzy_key": filename.lower(),
            "match_confidence": "fuzzy",
        },
    )
    q.link_rom_to_game(conn, rom_id, game_id)
    return rom_id


# ---------------------------------------------------------------------------
# ensure_favorites_collection
# ---------------------------------------------------------------------------


class TestEnsureFavorites:
    def test_creates_favorites_on_first_call(self, seeded_db) -> None:
        favorites_id = q.ensure_favorites_collection(seeded_db)
        row = seeded_db.execute(
            "SELECT name, is_system FROM collections WHERE id = ?",
            (favorites_id,),
        ).fetchone()
        assert row["name"] == q.FAVORITES_NAME
        assert int(row["is_system"]) == 1

    def test_idempotent(self, seeded_db) -> None:
        first = q.ensure_favorites_collection(seeded_db)
        second = q.ensure_favorites_collection(seeded_db)
        assert first == second
        count = seeded_db.execute(
            "SELECT COUNT(*) FROM collections WHERE name = ?",
            (q.FAVORITES_NAME,),
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# create_collection / delete_collection
# ---------------------------------------------------------------------------


class TestCreateCollection:
    def test_create_returns_new_id(self, seeded_db) -> None:
        cid = q.create_collection(seeded_db, "RPGs", "Role playing games")
        row = seeded_db.execute(
            "SELECT name, description, is_system FROM collections WHERE id = ?",
            (cid,),
        ).fetchone()
        assert row["name"] == "RPGs"
        assert row["description"] == "Role playing games"
        assert int(row["is_system"]) == 0

    def test_create_duplicate_name_raises(self, seeded_db) -> None:
        q.create_collection(seeded_db, "RPGs")
        with pytest.raises(sqlite3.IntegrityError):
            q.create_collection(seeded_db, "RPGs")


class TestDeleteCollection:
    def test_delete_user_collection_removes_row_and_members(
        self, seeded_db
    ) -> None:
        game_id = _add_game(seeded_db, "Chrono Trigger")
        cid = q.create_collection(seeded_db, "RPGs")
        q.add_game_to_collection(seeded_db, cid, game_id)
        assert q.is_game_in_collection(seeded_db, cid, game_id)

        q.delete_collection(seeded_db, cid)

        row = seeded_db.execute(
            "SELECT 1 FROM collections WHERE id = ?", (cid,)
        ).fetchone()
        assert row is None
        # Membership row is also gone.
        membership = seeded_db.execute(
            "SELECT 1 FROM collection_games WHERE collection_id = ?", (cid,)
        ).fetchone()
        assert membership is None

    def test_delete_unknown_id_is_a_noop(self, seeded_db) -> None:
        # Should not raise.
        q.delete_collection(seeded_db, 999999)

    def test_delete_system_collection_raises(self, seeded_db) -> None:
        favorites_id = q.ensure_favorites_collection(seeded_db)
        with pytest.raises(ValueError):
            q.delete_collection(seeded_db, favorites_id)


# ---------------------------------------------------------------------------
# add_game_to_collection / remove_game_from_collection
# ---------------------------------------------------------------------------


class TestMembership:
    def test_add_is_idempotent(self, seeded_db) -> None:
        game_id = _add_game(seeded_db, "Super Metroid")
        cid = q.create_collection(seeded_db, "Metroidvania")
        q.add_game_to_collection(seeded_db, cid, game_id)
        q.add_game_to_collection(seeded_db, cid, game_id)
        # Only one row in collection_games for this (cid, game_id).
        count = seeded_db.execute(
            "SELECT COUNT(*) FROM collection_games "
            "WHERE collection_id = ? AND game_id = ?",
            (cid, game_id),
        ).fetchone()[0]
        assert count == 1

    def test_remove_then_re_add(self, seeded_db) -> None:
        game_id = _add_game(seeded_db, "Castlevania IV")
        cid = q.create_collection(seeded_db, "Action")
        q.add_game_to_collection(seeded_db, cid, game_id)
        assert q.is_game_in_collection(seeded_db, cid, game_id)
        q.remove_game_from_collection(seeded_db, cid, game_id)
        assert not q.is_game_in_collection(seeded_db, cid, game_id)
        q.add_game_to_collection(seeded_db, cid, game_id)
        assert q.is_game_in_collection(seeded_db, cid, game_id)

    def test_get_collection_games_returns_member_ids(self, seeded_db) -> None:
        cid = q.create_collection(seeded_db, "Best of SNES")
        g1 = _add_game(seeded_db, "Mario")
        g2 = _add_game(seeded_db, "Zelda")
        q.add_game_to_collection(seeded_db, cid, g1)
        q.add_game_to_collection(seeded_db, cid, g2)
        ids = q.get_collection_games(seeded_db, cid)
        assert set(ids) == {g1, g2}

    def test_get_collection_games_empty(self, seeded_db) -> None:
        cid = q.create_collection(seeded_db, "Empty")
        assert q.get_collection_games(seeded_db, cid) == []


# ---------------------------------------------------------------------------
# get_collections — system rows first, with counts
# ---------------------------------------------------------------------------


class TestGetCollections:
    def test_lists_system_collections_first(self, seeded_db) -> None:
        favorites_id = q.ensure_favorites_collection(seeded_db)
        user_id = q.create_collection(seeded_db, "AAA")
        rows = q.get_collections(seeded_db)
        # Favorites (is_system=1) sorts ahead of any user collection.
        assert int(rows[0]["id"]) == favorites_id
        assert int(rows[1]["id"]) == user_id

    def test_game_count_aggregate(self, seeded_db) -> None:
        cid = q.create_collection(seeded_db, "Shooters")
        g1 = _add_game(seeded_db, "Contra")
        g2 = _add_game(seeded_db, "Gradius")
        q.add_game_to_collection(seeded_db, cid, g1)
        q.add_game_to_collection(seeded_db, cid, g2)
        rows = q.get_collections(seeded_db)
        for row in rows:
            if int(row["id"]) == cid:
                assert int(row["game_count"]) == 2
                return
        pytest.fail("expected collection row not present")

    def test_empty_collection_reports_zero_count(self, seeded_db) -> None:
        cid = q.create_collection(seeded_db, "Wishlist")
        rows = {
            int(r["id"]): int(r["game_count"]) for r in q.get_collections(seeded_db)
        }
        assert rows[cid] == 0


# ---------------------------------------------------------------------------
# Favorite toggle round-trip (mirrors DetailPanel's ★ button behavior)
# ---------------------------------------------------------------------------


class TestFavoriteToggle:
    def test_toggle_adds_then_removes(self, seeded_db) -> None:
        favorites_id = q.ensure_favorites_collection(seeded_db)
        game_id = _add_game(seeded_db, "Earthbound")
        _add_rom_for_game(seeded_db, game_id, "Earthbound.sfc")
        assert not q.is_game_in_collection(seeded_db, favorites_id, game_id)
        q.add_game_to_collection(seeded_db, favorites_id, game_id)
        assert q.is_game_in_collection(seeded_db, favorites_id, game_id)
        q.remove_game_from_collection(seeded_db, favorites_id, game_id)
        assert not q.is_game_in_collection(seeded_db, favorites_id, game_id)


# ---------------------------------------------------------------------------
# Detail-panel-supporting queries: get_game_by_id / get_roms_for_game
# ---------------------------------------------------------------------------


class TestDetailLookups:
    def test_get_game_by_id(self, seeded_db) -> None:
        game_id = _add_game(seeded_db, "Final Fantasy VI")
        row = q.get_game_by_id(seeded_db, game_id)
        assert row is not None
        assert row["title"] == "Final Fantasy VI"

    def test_get_game_by_id_missing(self, seeded_db) -> None:
        assert q.get_game_by_id(seeded_db, 999999) is None

    def test_get_roms_for_game(self, seeded_db) -> None:
        game_id = _add_game(seeded_db, "Mega Man X")
        _add_rom_for_game(seeded_db, game_id, "Mega Man X (USA).sfc")
        _add_rom_for_game(seeded_db, game_id, "Mega Man X (Japan).sfc")
        roms = q.get_roms_for_game(seeded_db, game_id)
        assert {row["filename"] for row in roms} == {
            "Mega Man X (USA).sfc",
            "Mega Man X (Japan).sfc",
        }
