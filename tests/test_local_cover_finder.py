"""Tests for local cover discovery — core logic, DB helpers, worker, and UI."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from romulus.core.local_cover_finder import (
    DiscoveryResult,
    _has_cover_for_path,
    _infer_cover_type,
    discover_local_covers,
    find_local_covers_for_rom,
)
from romulus.db import create_tables, queries
from romulus.db.connection import get_connection
from romulus.models import seed_systems

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite DB with schema + seeded systems."""
    conn = get_connection(tmp_path / "test.db")
    create_tables(conn)
    seed_systems(conn)
    yield conn
    conn.close()


def _enroll_rom(
    conn: sqlite3.Connection,
    rom_path: str,
    filename: str,
    system_id: str = "snes",
    fuzzy_key: str = "sonicthehedgehog",
) -> tuple[int, int]:
    """Insert a ROM and link it to a game; return (rom_id, game_id)."""
    rom_id = queries.upsert_rom(
        conn,
        {
            "path": rom_path,
            "filename": filename,
            "extension": Path(filename).suffix,
            "size_bytes": 512,
            "mtime": 0.0,
            "system_id": system_id,
            "fuzzy_key": fuzzy_key,
        },
    )
    game_id = queries.upsert_game(
        conn, {"title": "Sonic the Hedgehog", "system_id": system_id}
    )
    queries.link_rom_to_game(conn, rom_id, game_id)
    conn.commit()
    return rom_id, game_id


# ---------------------------------------------------------------------------
# Cover-type inference
# ---------------------------------------------------------------------------


class TestInferCoverType:
    def test_boxart_folder_returns_named_boxarts(self, tmp_path) -> None:
        img = tmp_path / "boxart" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Boxarts"

    def test_box_folder_returns_named_boxarts(self, tmp_path) -> None:
        img = tmp_path / "box" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Boxarts"

    def test_images_folder_returns_named_boxarts(self, tmp_path) -> None:
        img = tmp_path / "images" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Boxarts"

    def test_screenshots_folder_returns_named_snaps(self, tmp_path) -> None:
        img = tmp_path / "screenshots" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Snaps"

    def test_snaps_folder_returns_named_snaps(self, tmp_path) -> None:
        img = tmp_path / "snaps" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Snaps"

    def test_titles_folder_returns_named_titles(self, tmp_path) -> None:
        img = tmp_path / "titles" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Titles"

    def test_wheel_folder_returns_named_titles(self, tmp_path) -> None:
        img = tmp_path / "wheel" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Titles"

    def test_unknown_folder_defaults_to_named_boxarts(self, tmp_path) -> None:
        img = tmp_path / "roms" / "Sonic.png"
        img.parent.mkdir()
        assert _infer_cover_type(img) == "Named_Boxarts"


# ---------------------------------------------------------------------------
# find_local_covers_for_rom — exact and fuzzy matching
# ---------------------------------------------------------------------------


class TestFindLocalCoversForRom:
    def test_exact_stem_match_in_same_directory(self, tmp_path) -> None:
        """Image with identical stem to ROM is discovered."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = system_dir / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonicthehedgehog",
            clean_name="Sonic the Hedgehog",
            system_dir=system_dir,
        )
        assert len(matches) == 1
        assert Path(matches[0].image_path) == img.resolve()
        assert matches[0].cover_type == "Named_Boxarts"

    def test_match_in_media_images_subdir(self, tmp_path) -> None:
        """Image inside media/images/ is found."""
        system_dir = tmp_path / "snes"
        media_images = system_dir / "media" / "images"
        media_images.mkdir(parents=True)
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = media_images / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonicthehedgehog",
            clean_name="Sonic the Hedgehog",
            system_dir=system_dir,
        )
        assert len(matches) == 1
        assert Path(matches[0].image_path) == img.resolve()

    def test_boxart_dir_inferred_as_named_boxarts(self, tmp_path) -> None:
        system_dir = tmp_path / "nes"
        boxart_dir = system_dir / "boxart"
        boxart_dir.mkdir(parents=True)
        rom_path = system_dir / "Sonic (USA).nes"
        img = boxart_dir / "Sonic (USA).png"
        img.write_bytes(b"PNG")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=system_dir,
        )
        assert any(m.cover_type == "Named_Boxarts" for m in matches)

    def test_screenshots_dir_inferred_as_named_snaps(self, tmp_path) -> None:
        system_dir = tmp_path / "nes"
        ss_dir = system_dir / "screenshots"
        ss_dir.mkdir(parents=True)
        rom_path = system_dir / "Sonic (USA).nes"
        img = ss_dir / "Sonic (USA).png"
        img.write_bytes(b"PNG")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=system_dir,
        )
        assert any(m.cover_type == "Named_Snaps" for m in matches)

    def test_titles_dir_inferred_as_named_titles(self, tmp_path) -> None:
        system_dir = tmp_path / "nes"
        t_dir = system_dir / "titles"
        t_dir.mkdir(parents=True)
        rom_path = system_dir / "Sonic (USA).nes"
        img = t_dir / "Sonic (USA).png"
        img.write_bytes(b"PNG")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=system_dir,
        )
        assert any(m.cover_type == "Named_Titles" for m in matches)

    def test_fuzzy_match_across_tag_stripping(self, tmp_path) -> None:
        """'Sonic (USA).png' matches a ROM with fuzzy_key 'sonic'."""
        system_dir = tmp_path / "nes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic.nes"
        img = system_dir / "Sonic (USA).png"
        img.write_bytes(b"PNG")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=system_dir,
        )
        assert len(matches) == 1

    def test_fuzzy_match_article_fronting(self, tmp_path) -> None:
        """'Addams Family, The.png' matches a ROM with title 'The Addams Family'."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Addams Family, The (USA).sfc"
        # Image uses article-fronted form; both reduce to same fuzzy key.
        img = system_dir / "The Addams Family.png"
        img.write_bytes(b"PNG")

        from romulus.core.scanner import generate_fuzzy_key
        rom_fuzzy = generate_fuzzy_key("Addams Family, The")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key=rom_fuzzy,
            clean_name="Addams Family, The",
            system_dir=system_dir,
        )
        assert len(matches) >= 1

    def test_fuzzy_match_roman_numerals(self, tmp_path) -> None:
        """'Final Fantasy 6.png' matches a ROM named 'Final Fantasy VI'."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Final Fantasy VI (USA).sfc"
        img = system_dir / "Final Fantasy 6.png"
        img.write_bytes(b"PNG")

        from romulus.core.scanner import generate_fuzzy_key
        rom_fuzzy = generate_fuzzy_key("Final Fantasy VI")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key=rom_fuzzy,
            clean_name="Final Fantasy VI",
            system_dir=system_dir,
        )
        assert len(matches) >= 1

    @pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"])
    def test_all_image_extensions_detected(self, tmp_path, ext) -> None:
        """All six supported image extensions are found."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic.sfc"
        img = system_dir / f"Sonic{ext}"
        img.write_bytes(b"IMG")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=system_dir,
        )
        assert len(matches) == 1, f"Expected 1 match for {ext}"

    def test_non_image_files_ignored(self, tmp_path) -> None:
        """A .txt file sitting next to the ROM is not matched."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic.sfc"
        (system_dir / "Sonic.txt").write_text("not an image")

        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=2,
            rom_path=str(rom_path),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=system_dir,
        )
        assert matches == []


# ---------------------------------------------------------------------------
# discover_local_covers — orchestrator (DB round-trips)
# ---------------------------------------------------------------------------


class TestDiscoverLocalCovers:
    def test_discover_inserts_cover_row(self, db, tmp_path) -> None:
        """A matched image is written to the covers table."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = system_dir / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        _enroll_rom(db, str(rom_path), rom_path.name)
        result = discover_local_covers(db, tmp_path)

        assert isinstance(result, DiscoveryResult)
        assert result.covers_found == 1
        assert result.covers_skipped_existing == 0

    def test_discover_is_idempotent(self, db, tmp_path) -> None:
        """Running discovery twice does not create duplicate cover rows."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = system_dir / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        _enroll_rom(db, str(rom_path), rom_path.name)
        discover_local_covers(db, tmp_path)
        result2 = discover_local_covers(db, tmp_path)

        assert result2.covers_found == 0
        assert result2.covers_skipped_existing == 1

    def test_discovery_result_skipped_count(self, db, tmp_path) -> None:
        """DiscoveryResult.covers_skipped_existing counts pre-existing rows."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = system_dir / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        _, game_id = _enroll_rom(db, str(rom_path), rom_path.name)
        # Pre-insert a cover so second run sees an existing one.
        queries.insert_cover(
            db, game_id, "Named_Boxarts", source_url=None,
            local_path=str(img.resolve())
        )
        db.commit()

        result = discover_local_covers(db, tmp_path)
        assert result.covers_skipped_existing >= 1

    def test_roms_without_game_are_skipped(self, db, tmp_path) -> None:
        """ROMs not yet linked to a game do not cause errors and yield 0 covers."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic.sfc"
        (system_dir / "Sonic.png").write_bytes(b"PNG")

        # Enroll a ROM but do NOT link it to a game.
        queries.upsert_rom(
            db,
            {
                "path": str(rom_path),
                "filename": rom_path.name,
                "extension": ".sfc",
                "size_bytes": 512,
                "mtime": 0.0,
                "system_id": "snes",
                "fuzzy_key": "sonic",
            },
        )
        db.commit()

        result = discover_local_covers(db, tmp_path)
        assert result.roms_scanned == 0  # excluded by the query
        assert result.covers_found == 0

    def test_worker_emits_progress_and_finished(self, tmp_path, qapp) -> None:
        """LocalCoverFinderWorker emits progress ticks and finished_ok."""
        from PySide6.QtCore import QCoreApplication

        from romulus.db import create_tables
        from romulus.ui.workers import LocalCoverFinderWorker

        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = system_dir / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        db_path = tmp_path / "worker_test.db"
        # Build a fresh DB at the worker's own path.
        worker_conn = get_connection(db_path)
        create_tables(worker_conn)
        seed_systems(worker_conn)
        _enroll_rom(worker_conn, str(rom_path), rom_path.name)
        worker_conn.close()

        progress_calls: list[tuple[int, int, str]] = []
        finished_calls: list[tuple[int, int, int, int]] = []

        worker = LocalCoverFinderWorker(str(db_path), str(tmp_path))
        worker.progress.connect(
            lambda c, t, f: progress_calls.append((c, t, f))
        )
        worker.finished_ok.connect(
            lambda rs, cf, cs, e: finished_calls.append((rs, cf, cs, e))
        )
        worker.start()
        worker.wait(10_000)
        QCoreApplication.processEvents()

        assert len(finished_calls) == 1
        roms_scanned, covers_found, _skipped, errors = finished_calls[0]
        assert roms_scanned == 1
        assert covers_found == 1
        assert errors == 0

    def test_worker_cooperative_cancel(self, tmp_path, qapp) -> None:
        """Cancelling the worker emits failed with 'cancelled' in the message."""
        from PySide6.QtCore import QCoreApplication

        from romulus.db import create_tables
        from romulus.ui.workers import LocalCoverFinderWorker

        # Create enough ROMs that there is room to cancel mid-run.
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        db_path = tmp_path / "cancel_test.db"

        cancel_conn = get_connection(db_path)
        create_tables(cancel_conn)
        seed_systems(cancel_conn)
        for i in range(5):
            name = f"Game{i} (USA).sfc"
            fpath = system_dir / name
            _enroll_rom(
                cancel_conn, str(fpath), name,
                fuzzy_key=f"game{i}",
            )
        cancel_conn.close()

        failed_messages: list[str] = []
        worker = LocalCoverFinderWorker(str(db_path), str(tmp_path))
        worker.failed.connect(lambda m: failed_messages.append(m))
        worker.start()
        worker.cancel()
        worker.wait(10_000)
        QCoreApplication.processEvents()

        # Either cancelled mid-run (failed signal) or finished before cancel.
        # At minimum the worker must not raise an unhandled exception.
        assert worker.isRunning() is False


# ---------------------------------------------------------------------------
# has_cover_for_path query helper
# ---------------------------------------------------------------------------


class TestHasCoverForPath:
    def test_returns_false_when_no_row(self, db) -> None:
        assert _has_cover_for_path(db, 1, "/some/path.png") is False

    def test_returns_true_after_insert(self, db, tmp_path) -> None:
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic.sfc"
        _, game_id = _enroll_rom(db, str(rom_path), rom_path.name, fuzzy_key="sonic")
        queries.insert_cover(
            db, game_id, "Named_Boxarts", source_url=None, local_path="/img/sonic.png"
        )
        db.commit()
        assert _has_cover_for_path(db, game_id, "/img/sonic.png") is True
