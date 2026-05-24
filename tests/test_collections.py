"""Tests for the collections system — queries + favorite toggle behavior.

These tests exercise the SQL layer (queries.py) only. The UI side (the
DetailPanel star button, the right-click submenu) is covered in test_ui.py
where the qapp fixture is available.

Updated for strict 1:1 rom ↔ game model (v0.4.0):
- Collection membership is now keyed on rom_id, not game_id.
- The collection_roms table replaces collection_games.
- get_collection_roms() returns rom ids; get_collections() reports rom_count.
- get_rom_by_id() replaces get_game_by_id().
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from romulus.db import queries as q


def _add_rom(
    conn: sqlite3.Connection,
    title: str,
    system_id: str = "snes",
) -> int:
    """Insert a minimal ``roms`` row and return its id."""
    return q.upsert_rom(
        conn,
        {
            "path": f"/library/{system_id}/{title}.sfc",
            "filename": f"{title}.sfc",
            "extension": ".sfc",
            "size_bytes": 1024,
            "mtime": time.time(),
            "system_id": system_id,
            "fuzzy_key": title.lower().replace(" ", ""),
            "match_confidence": "fuzzy",
        },
    )


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
        """Deleting a collection also removes its collection_roms membership rows."""
        rom_id = _add_rom(seeded_db, "Chrono Trigger")
        cid = q.create_collection(seeded_db, "RPGs")
        q.add_rom_to_collection(seeded_db, cid, rom_id)
        assert q.is_rom_in_collection(seeded_db, cid, rom_id)

        q.delete_collection(seeded_db, cid)

        row = seeded_db.execute(
            "SELECT 1 FROM collections WHERE id = ?", (cid,)
        ).fetchone()
        assert row is None
        # Membership row is also gone.
        membership = seeded_db.execute(
            "SELECT 1 FROM collection_roms WHERE collection_id = ?", (cid,)
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
# add_rom_to_collection / remove_rom_from_collection
# ---------------------------------------------------------------------------


class TestMembership:
    def test_add_is_idempotent(self, seeded_db) -> None:
        """Adding the same ROM twice must produce exactly one membership row."""
        rom_id = _add_rom(seeded_db, "Super Metroid")
        cid = q.create_collection(seeded_db, "Metroidvania")
        q.add_rom_to_collection(seeded_db, cid, rom_id)
        q.add_rom_to_collection(seeded_db, cid, rom_id)
        # Only one row in collection_roms for this (cid, rom_id).
        count = seeded_db.execute(
            "SELECT COUNT(*) FROM collection_roms "
            "WHERE collection_id = ? AND rom_id = ?",
            (cid, rom_id),
        ).fetchone()[0]
        assert count == 1

    def test_remove_then_re_add(self, seeded_db) -> None:
        """Remove + re-add round-trip must leave membership in place."""
        rom_id = _add_rom(seeded_db, "Castlevania IV")
        cid = q.create_collection(seeded_db, "Action")
        q.add_rom_to_collection(seeded_db, cid, rom_id)
        assert q.is_rom_in_collection(seeded_db, cid, rom_id)
        q.remove_rom_from_collection(seeded_db, cid, rom_id)
        assert not q.is_rom_in_collection(seeded_db, cid, rom_id)
        q.add_rom_to_collection(seeded_db, cid, rom_id)
        assert q.is_rom_in_collection(seeded_db, cid, rom_id)

    def test_get_collection_roms_returns_member_ids(self, seeded_db) -> None:
        """get_collection_roms returns the rom ids that were added."""
        cid = q.create_collection(seeded_db, "Best of SNES")
        rid1 = _add_rom(seeded_db, "Mario")
        rid2 = _add_rom(seeded_db, "Zelda")
        q.add_rom_to_collection(seeded_db, cid, rid1)
        q.add_rom_to_collection(seeded_db, cid, rid2)
        ids = q.get_collection_roms(seeded_db, cid)
        assert set(ids) == {rid1, rid2}

    def test_get_collection_roms_empty(self, seeded_db) -> None:
        """A newly created collection returns an empty list."""
        cid = q.create_collection(seeded_db, "Empty")
        assert q.get_collection_roms(seeded_db, cid) == []


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

    def test_rom_count_aggregate(self, seeded_db) -> None:
        """rom_count on get_collections reflects the number of rom memberships."""
        cid = q.create_collection(seeded_db, "Shooters")
        rid1 = _add_rom(seeded_db, "Contra")
        rid2 = _add_rom(seeded_db, "Gradius")
        q.add_rom_to_collection(seeded_db, cid, rid1)
        q.add_rom_to_collection(seeded_db, cid, rid2)
        rows = q.get_collections(seeded_db)
        for row in rows:
            if int(row["id"]) == cid:
                assert int(row["rom_count"]) == 2
                return
        pytest.fail("expected collection row not present")

    def test_empty_collection_reports_zero_count(self, seeded_db) -> None:
        """An empty collection must report rom_count = 0."""
        cid = q.create_collection(seeded_db, "Wishlist")
        rows = {
            int(r["id"]): int(r["rom_count"]) for r in q.get_collections(seeded_db)
        }
        assert rows[cid] == 0


# ---------------------------------------------------------------------------
# Favorite toggle round-trip (mirrors DetailPanel star button behavior)
# ---------------------------------------------------------------------------


class TestFavoriteToggle:
    def test_toggle_adds_then_removes(self, seeded_db) -> None:
        """Adding and removing a ROM from Favorites round-trips correctly."""
        favorites_id = q.ensure_favorites_collection(seeded_db)
        rom_id = _add_rom(seeded_db, "Earthbound")
        assert not q.is_rom_in_collection(seeded_db, favorites_id, rom_id)
        q.add_rom_to_collection(seeded_db, favorites_id, rom_id)
        assert q.is_rom_in_collection(seeded_db, favorites_id, rom_id)
        q.remove_rom_from_collection(seeded_db, favorites_id, rom_id)
        assert not q.is_rom_in_collection(seeded_db, favorites_id, rom_id)


# ---------------------------------------------------------------------------
# Detail-panel-supporting queries: get_rom_by_id
# ---------------------------------------------------------------------------


class TestDetailLookups:
    def test_get_rom_by_id(self, seeded_db) -> None:
        """get_rom_by_id returns the rom row for an existing id."""
        rom_id = _add_rom(seeded_db, "Final Fantasy VI")
        row = q.get_rom_by_id(seeded_db, rom_id)
        assert row is not None
        assert row["filename"] == "Final Fantasy VI.sfc"

    def test_get_rom_by_id_missing(self, seeded_db) -> None:
        """get_rom_by_id returns None for an unknown id."""
        assert q.get_rom_by_id(seeded_db, 999999) is None

    def test_rom_has_all_identity_fields(self, seeded_db) -> None:
        """In the 1:1 model the rom row carries all identity fields directly."""
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Mega Man X (USA).sfc",
                "filename": "Mega Man X (USA).sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
                "fuzzy_key": "megamanx",
                "region": "USA",
                "match_confidence": "dat_verified",
            },
        )
        row = q.get_rom_by_id(seeded_db, rom_id)
        assert row is not None
        assert row["region"] == "USA"
        assert row["match_confidence"] == "dat_verified"
