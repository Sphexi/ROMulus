"""Tests for the destination inventory walker (sync-design §4.2, §4.5).

Covers cache reuse semantics, signature drift / re-recognition, the
"Forget cache" query, depth-cap behaviour, and empty/large-dir handling.
The walker is a pure read pass — every test stages files on disk under
``tmp_path``, runs :func:`scan_destination`, and asserts on the resulting
:class:`DestInventory` and the underlying ``dest_inventory`` rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from romulus.core.dest_inventory import (
    DestInventory,
    InventoryEntry,
    compute_signature,
    forget_cache,
    scan_destination,
    signature_matches,
)
from romulus.db import queries as q


def _make_dest(conn: sqlite3.Connection, target: Path, name: str = "Test") -> int:
    """Create a saved destination row and return its id."""
    return q.insert_sync_destination(
        conn,
        {"name": name, "target_path": str(target), "profile_id": "test"},
    )


def _write_file(path: Path, content: bytes = b"x" * 64) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestSignatureHelpers:
    def test_compute_signature_is_stable_under_reordering(self) -> None:
        # Sorted-first means input order doesn't affect output.
        s1 = compute_signature(["a", "b", "c"])
        s2 = compute_signature(["c", "a", "b"])
        assert s1 == s2

    def test_compute_signature_empty_string_for_empty_input(self) -> None:
        assert compute_signature([]) == ""

    def test_compute_signature_caps_at_32_paths(self) -> None:
        thirty_two = [f"path/{i:02d}.bin" for i in range(32)]
        thirty_three = thirty_two + ["path/extra.bin"]
        # The signature only folds the first 32 entries — adding a 33rd
        # (sorted higher than the rest) shouldn't change the signature.
        assert compute_signature(thirty_two) == compute_signature(thirty_three)

    def test_signature_matches_treats_none_as_first_visit(self) -> None:
        assert signature_matches(None, "abc") is True
        assert signature_matches("", "abc") is True

    def test_signature_matches_detects_drift(self) -> None:
        assert signature_matches("abc", "xyz") is False
        assert signature_matches("abc", "abc") is True


class TestEmptyAndMissing:
    def test_missing_target_returns_empty_inventory(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        dest_id = _make_dest(db, tmp_path / "missing")
        result = scan_destination(db, dest_id, tmp_path / "missing")
        assert isinstance(result, DestInventory)
        assert result.entries == []
        assert result.signature == ""

    def test_empty_dir_scan_no_rows(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "empty"
        target.mkdir()
        dest_id = _make_dest(db, target)
        result = scan_destination(db, dest_id, target)
        assert result.entries == []
        # signature for empty input is "".
        assert result.signature == ""
        cached = q.get_dest_inventory(db, dest_id)
        assert cached == []


class TestWalkAndCache:
    def test_walks_target_and_records_entries(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "snes" / "Game.sfc", b"snes-rom")
        _write_file(target / "nes" / "Game.nes", b"nes-rom")
        dest_id = _make_dest(db, target)
        result = scan_destination(db, dest_id, target)
        rel_paths = sorted(e.rel_path for e in result.entries)
        assert rel_paths == ["nes/Game.nes", "snes/Game.sfc"]
        cached = {row["rel_path"] for row in q.get_dest_inventory(db, dest_id)}
        assert cached == set(rel_paths)

    def test_size_total_summed(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc", b"a" * 100)
        _write_file(target / "b.sfc", b"b" * 200)
        dest_id = _make_dest(db, target)
        result = scan_destination(db, dest_id, target)
        assert result.total_size_bytes == 300

    def test_signature_is_stamped_after_scan(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc")
        dest_id = _make_dest(db, target)
        scan_destination(db, dest_id, target)
        row = q.get_sync_destination(db, dest_id)
        assert row is not None
        assert row["last_inventory_signature"]

    def test_cache_reuse_on_unchanged_file(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc", b"x" * 64)
        dest_id = _make_dest(db, target)
        # Pre-populate a SHA-1 in the cache to simulate a previous deep verify.
        q.upsert_dest_inventory(
            db,
            {
                "dest_id": dest_id,
                "rel_path": "a.sfc",
                "size_bytes": 64,
                "mtime": (target / "a.sfc").stat().st_mtime,
                "sha1": "deadbeef",
            },
        )
        db.commit()
        # Re-scan without deep verify — cached SHA-1 should survive.
        result = scan_destination(db, dest_id, target, deep_verify=False)
        entry = result.entries[0]
        assert entry.sha1 == "deadbeef"

    def test_size_or_mtime_drift_clears_cached_rom_id(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        path = target / "a.sfc"
        _write_file(path, b"original")
        dest_id = _make_dest(db, target)
        scan_destination(db, dest_id, target)
        # Stamp a cached SHA-1 then mutate the file so size drifts.
        q.upsert_dest_inventory(
            db,
            {
                "dest_id": dest_id,
                "rel_path": "a.sfc",
                "size_bytes": path.stat().st_size,
                "mtime": path.stat().st_mtime,
                "sha1": "abc123",
            },
        )
        db.commit()
        path.write_bytes(b"changed content - bigger and bigger")  # size + mtime drift
        result = scan_destination(db, dest_id, target)
        entry = result.entries[0]
        # The cached SHA-1 should be cleared by the staleness check.
        assert entry.sha1 is None


class TestSignatureDrift:
    def test_signature_drift_clears_cache(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # First scan populates the signature.
        target = tmp_path / "dest"
        _write_file(target / "a.sfc")
        _write_file(target / "b.sfc")
        dest_id = _make_dest(db, target)
        scan_destination(db, dest_id, target)
        # Pretend the user mounted a different SD card: same dest_id, totally
        # different rel_paths. Wipe the existing files and write new ones.
        for child in target.glob("*"):
            child.unlink()
        _write_file(target / "different_a.bin")
        _write_file(target / "different_b.bin")
        result = scan_destination(db, dest_id, target)
        assert result.cache_was_invalidated is True
        rel_paths = sorted(e.rel_path for e in result.entries)
        assert rel_paths == ["different_a.bin", "different_b.bin"]

    def test_signature_stable_means_cache_reused(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc")
        _write_file(target / "b.sfc")
        dest_id = _make_dest(db, target)
        scan_destination(db, dest_id, target)
        result = scan_destination(db, dest_id, target)
        assert result.cache_was_invalidated is False


class TestPrune:
    def test_pruning_removes_vanished_rows(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc")
        _write_file(target / "b.sfc")
        dest_id = _make_dest(db, target)
        scan_destination(db, dest_id, target)
        # Remove one file; re-scan; cache should drop it.
        (target / "a.sfc").unlink()
        scan_destination(db, dest_id, target)
        cached = {row["rel_path"] for row in q.get_dest_inventory(db, dest_id)}
        assert cached == {"b.sfc"}


class TestForgetCache:
    def test_forget_cache_clears_rows_and_signature(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc")
        dest_id = _make_dest(db, target)
        scan_destination(db, dest_id, target)
        # Sanity: rows + signature are present before forgetting.
        assert q.get_dest_inventory(db, dest_id)
        before = q.get_sync_destination(db, dest_id)
        assert before is not None
        assert before["last_inventory_signature"]
        forget_cache(db, dest_id)
        after = q.get_sync_destination(db, dest_id)
        assert after is not None
        assert not after["last_inventory_signature"]
        assert q.get_dest_inventory(db, dest_id) == []


class TestDepthCap:
    def test_depth_cap_stops_recursion(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        # Build a deep tree: target/a/b/c/d/e/file.sfc — 5 levels deep.
        path = target / "a" / "b" / "c" / "d" / "e" / "file.sfc"
        _write_file(path)
        # Also a shallow file so the walker has something to find.
        _write_file(target / "shallow.sfc")
        dest_id = _make_dest(db, target)
        result = scan_destination(db, dest_id, target, depth_cap=2)
        rel_paths = sorted(e.rel_path for e in result.entries)
        # Only the shallow file should be reported — the deep one is below
        # the cap.
        assert "shallow.sfc" in rel_paths
        assert all("a/b/c" not in p for p in rel_paths)


class TestDeepVerify:
    def test_deep_verify_populates_sha1(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc", b"deterministic-bytes")
        dest_id = _make_dest(db, target)
        result = scan_destination(db, dest_id, target, deep_verify=True)
        entry = result.entries[0]
        assert entry.sha1 is not None
        assert len(entry.sha1) == 40  # SHA-1 hex digest

    def test_deep_verify_reuse_after_initial(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        target = tmp_path / "dest"
        _write_file(target / "a.sfc", b"x")
        dest_id = _make_dest(db, target)
        first = scan_destination(db, dest_id, target, deep_verify=True)
        # Second pass without deep_verify should preserve the cached SHA-1.
        second = scan_destination(db, dest_id, target, deep_verify=False)
        assert first.entries[0].sha1 == second.entries[0].sha1


class TestEntryDataclass:
    def test_inventory_entry_is_frozen(self) -> None:
        entry = InventoryEntry(rel_path="x", size_bytes=1, mtime=1.0)
        # frozen dataclasses raise FrozenInstanceError (a subclass of
        # AttributeError) when a slot is reassigned.
        with pytest.raises(AttributeError):
            entry.size_bytes = 2  # type: ignore[misc]

    def test_by_rel_path_view(self) -> None:
        inv = DestInventory(
            dest_id=1,
            target_path="/tmp",
            entries=[
                InventoryEntry(rel_path="a", size_bytes=1, mtime=0.0),
                InventoryEntry(rel_path="b", size_bytes=2, mtime=0.0),
            ],
        )
        view = inv.by_rel_path()
        assert view["a"].size_bytes == 1
        assert view["b"].size_bytes == 2
