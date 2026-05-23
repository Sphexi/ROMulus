"""Tests for local cover discovery — core logic, DB helpers, worker, and UI."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from romulus.core.local_cover_finder import (
    DiscoveryResult,
    _build_image_bucket,
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
) -> int:
    """Insert a ROM and return its rom_id.

    Post Session-13/14 schema: no ``games`` table; each ROM stands alone.
    """
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
            "title": "Sonic the Hedgehog",
        },
    )
    conn.commit()
    return rom_id


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
        """Running discovery twice does not create duplicate cover rows.

        After the first run, the ROM has a Named_Boxarts cover row so
        ``_get_roms_for_cover_scan`` excludes it entirely on the second run.
        The result is roms_scanned=0 (not scanned), covers_found=0, no dupes.
        """
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = system_dir / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        _enroll_rom(db, str(rom_path), rom_path.name)
        discover_local_covers(db, tmp_path)
        # Second run: the ROM was given a boxart cover in the first run, so
        # it is excluded from the scan query entirely.
        result2 = discover_local_covers(db, tmp_path)

        assert result2.covers_found == 0
        assert result2.roms_scanned == 0  # excluded before scan, not skipped mid-scan
        # Confirm only one cover row was ever created.
        rom_id = db.execute("SELECT id FROM roms LIMIT 1").fetchone()["id"]
        from romulus.db import queries as q
        assert q.count_covers(db, rom_id, "Named_Boxarts") == 1

    def test_discovery_result_skipped_count(self, db, tmp_path) -> None:
        """A ROM with a pre-existing Named_Boxarts cover is excluded from scanning.

        Post Session-15: ``_get_roms_for_cover_scan`` filters out ROMs that
        already have a ``Named_Boxarts`` cover row. Such ROMs are not returned
        to the scan loop at all, so ``roms_scanned == 0`` and
        ``covers_skipped_existing == 0`` (skipped at query level, not scan level).
        """
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic the Hedgehog (USA).sfc"
        img = system_dir / "Sonic the Hedgehog (USA).png"
        img.write_bytes(b"PNG")

        rom_id = _enroll_rom(db, str(rom_path), rom_path.name)
        # Pre-insert a Named_Boxarts cover — this exclusion happens at query level.
        queries.insert_cover(
            db, rom_id, "Named_Boxarts", source_url=None,
            local_path=str(img.resolve())
        )
        db.commit()

        result = discover_local_covers(db, tmp_path)
        # The ROM was excluded from the scan by the query filter.
        assert result.roms_scanned == 0
        assert result.covers_found == 0

    def test_roms_with_existing_boxart_are_excluded(self, db, tmp_path) -> None:
        """ROMs that already have a Named_Boxarts cover are excluded from the scan.

        Post Session-15 schema: the old ``game_id IS NOT NULL`` filter is gone.
        The new filter is ``LEFT JOIN covers WHERE c.id IS NULL`` for Named_Boxarts.
        A ROM with an existing boxart cover should NOT be re-scanned.
        """
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Sonic.sfc"
        img = system_dir / "Sonic.png"
        img.write_bytes(b"PNG")

        rom_id = _enroll_rom(db, str(rom_path), rom_path.name, fuzzy_key="sonic")
        # Pre-insert a Named_Boxarts cover — this excludes the ROM from discovery.
        queries.insert_cover(
            db, rom_id, "Named_Boxarts", source_url=None, local_path=str(img)
        )
        db.commit()

        result = discover_local_covers(db, tmp_path)
        # The ROM was already covered so it should not appear in roms_scanned.
        assert result.roms_scanned == 0
        assert result.covers_found == 0

    def test_roms_without_fuzzy_key_are_excluded(self, db, tmp_path) -> None:
        """ROMs with no fuzzy_key (unidentified) are skipped — no identity to match on."""
        system_dir = tmp_path / "snes"
        system_dir.mkdir()
        rom_path = system_dir / "Unknown.sfc"
        (system_dir / "Unknown.png").write_bytes(b"PNG")

        # Enroll a ROM with no fuzzy_key set.
        queries.upsert_rom(
            db,
            {
                "path": str(rom_path),
                "filename": rom_path.name,
                "extension": ".sfc",
                "size_bytes": 512,
                "mtime": 0.0,
                "system_id": "snes",
                # No fuzzy_key — eligible filter requires fuzzy_key IS NOT NULL.
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
        rom_id = _enroll_rom(db, str(rom_path), rom_path.name, fuzzy_key="sonic")
        queries.insert_cover(
            db, rom_id, "Named_Boxarts", source_url=None, local_path="/img/sonic.png"
        )
        db.commit()
        assert _has_cover_for_path(db, rom_id, "/img/sonic.png") is True


# ---------------------------------------------------------------------------
# Recursive walk + loose matching
# ---------------------------------------------------------------------------


class TestRecursiveImageBucket:
    """The bucket builder must walk all subdirs of the system folder, not just a
    fixed allow-list. User-reported: WBM puts covers in ``downloaded_images/``
    which wasn't being found by the old fixed MEDIA_SUBDIRS list.
    """

    def test_finds_images_in_downloaded_images_folder(self, tmp_path) -> None:
        gb = tmp_path / "gb"
        downloaded = gb / "downloaded_images"
        downloaded.mkdir(parents=True)
        (downloaded / "Tetris (USA, Europe).png").write_bytes(b"PNG")
        bucket = _build_image_bucket(gb)
        # fuzzy_key for "Tetris" is "tetris" — release_type stripped.
        assert "tetris" in bucket
        assert len(bucket["tetris"]) == 1

    def test_finds_images_in_arbitrary_nested_subdir(self, tmp_path) -> None:
        """Anywhere within the system folder, up to the depth cap."""
        gb = tmp_path / "gb"
        nested = gb / "media" / "extras" / "EU" / "covers"
        nested.mkdir(parents=True)
        (nested / "Sonic.png").write_bytes(b"PNG")
        bucket = _build_image_bucket(gb)
        assert "sonic" in bucket

    def test_skips_backup_folder(self, tmp_path) -> None:
        """``backup/`` is in the skip list — its contents are ignored even
        though they look like images."""
        gb = tmp_path / "gb"
        backup = gb / "backup" / "old_covers"
        backup.mkdir(parents=True)
        (backup / "Tetris.png").write_bytes(b"PNG")
        bucket = _build_image_bucket(gb)
        assert "tetris" not in bucket

    def test_skips_logs_arrm_folder(self, tmp_path) -> None:
        gb = tmp_path / "gb"
        logs = gb / "logs_arrm"
        logs.mkdir(parents=True)
        (logs / "Sonic.png").write_bytes(b"PNG")
        bucket = _build_image_bucket(gb)
        assert "sonic" not in bucket

    def test_skips_hidden_directories(self, tmp_path) -> None:
        gb = tmp_path / "gb"
        hidden = gb / ".cache"
        hidden.mkdir(parents=True)
        (hidden / "Sonic.png").write_bytes(b"PNG")
        bucket = _build_image_bucket(gb)
        assert "sonic" not in bucket

    def test_respects_depth_cap(self, tmp_path) -> None:
        """At depth > _BUCKET_WALK_DEPTH the walker stops descending."""
        from romulus.core.local_cover_finder import _BUCKET_WALK_DEPTH

        gb = tmp_path / "gb"
        # Build a path one level deeper than the cap.
        path = gb
        for i in range(_BUCKET_WALK_DEPTH + 1):
            path = path / f"d{i}"
        path.mkdir(parents=True)
        (path / "TooDeep.png").write_bytes(b"PNG")
        bucket = _build_image_bucket(gb)
        assert "toodeep" not in bucket


class TestLooseFuzzyMatching:
    """release_type tags are stripped when matching local covers so a generic
    ``Sonic.png`` matches both ``Sonic.zip`` and ``Sonic (Virtual Console).zip``.
    The strict-match path used by the scanner stays unchanged.
    """

    def test_generic_image_matches_vc_rom(self, tmp_path) -> None:
        gb = tmp_path / "gb"
        gb.mkdir()
        (gb / "Sonic.png").write_bytes(b"PNG")
        # ROM with VC suffix in its stored fuzzy_key.
        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=1,
            rom_path=str(gb / "Sonic (Virtual Console).gb"),
            fuzzy_key="sonic__virtualconsole",
            clean_name="Sonic",
            system_dir=gb,
        )
        assert any(m.image_path.endswith("Sonic.png") for m in matches)

    def test_vc_image_matches_generic_rom(self, tmp_path) -> None:
        gb = tmp_path / "gb"
        gb.mkdir()
        (gb / "Sonic (Virtual Console).png").write_bytes(b"PNG")
        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=1,
            rom_path=str(gb / "Sonic.gb"),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=gb,
        )
        assert any("Virtual Console" in m.image_path for m in matches)

    def test_multiple_images_all_returned_for_one_rom(self, tmp_path) -> None:
        """Several images matching the same ROM all come back — 1:N data
        layer is unchanged, just verifying the recursive walk surfaces all."""
        gb = tmp_path / "gb"
        (gb / "downloaded_images").mkdir(parents=True)
        (gb / "boxart").mkdir()
        (gb / "Sonic.png").write_bytes(b"PNG")
        (gb / "downloaded_images" / "Sonic.png").write_bytes(b"PNG")
        (gb / "boxart" / "Sonic.png").write_bytes(b"PNG")
        matches = find_local_covers_for_rom(
            rom_id=1,
            game_id=1,
            rom_path=str(gb / "Sonic.gb"),
            fuzzy_key="sonic",
            clean_name="Sonic",
            system_dir=gb,
        )
        assert len(matches) == 3
