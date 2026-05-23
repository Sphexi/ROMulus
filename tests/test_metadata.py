"""Tests for metadata clients — libretro thumbnails, Hasheous, LaunchBox.

All HTTP traffic is intercepted via `httpx.MockTransport`. No real network
requests are issued from these tests.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from romulus.db import get_config, seed_defaults, set_config
from romulus.db import queries as q
from romulus.metadata import (
    enrich_library,
    gamedb,
    hasheous,
    launchbox,
    libretro,
    libretro_metadat,
    screenscraper,
    thegamesdb,
)

# ---------------------------------------------------------------------------
# libretro-thumbnails
# ---------------------------------------------------------------------------


class TestSanitizeGameName:
    def test_passthrough_safe_name(self) -> None:
        assert libretro.sanitize_game_name("Mario (USA).png") == "Mario (USA).png"

    def test_replaces_forbidden_chars(self) -> None:
        # Each forbidden char becomes "_"; spaces are kept.
        bad = "Foo&Bar*Baz/Qux:quux<a>b?c\\d|e\"end"
        out = libretro.sanitize_game_name(bad)
        for ch in '&*/:\\<>?|"':
            assert ch not in out
        assert out == "Foo_Bar_Baz_Qux_quux_a_b_c_d_e_end"

    def test_preserves_unicode(self) -> None:
        assert libretro.sanitize_game_name("Pokémon Ω") == "Pokémon Ω"

    def test_sanitize_chars_has_exactly_ten_entries(self) -> None:
        """The libretro-thumbnails spec lists 10 distinct forbidden chars;
        a future edit must not silently change the count.
        """
        assert len(libretro._SANITIZE_CHARS) == 10


class TestBuildThumbnailUrl:
    def test_snes_boxart_url(self) -> None:
        url = libretro.build_thumbnail_url(
            "Nintendo - Super Nintendo Entertainment System",
            "Super Mario World (USA)",
            "Named_Boxarts",
        )
        assert url == (
            "https://thumbnails.libretro.com/"
            "Nintendo%20-%20Super%20Nintendo%20Entertainment%20System/"
            "Named_Boxarts/Super%20Mario%20World%20%28USA%29.png"
        )

    def test_replaces_forbidden_chars_in_game(self) -> None:
        url = libretro.build_thumbnail_url(
            "Nintendo - Game Boy",
            "Foo*Bar",
            "Named_Snaps",
        )
        assert "Named_Snaps" in url
        assert url.endswith("Foo_Bar.png")

    def test_unknown_cover_type_raises(self) -> None:
        with pytest.raises(ValueError):
            libretro.build_thumbnail_url("Nintendo - Game Boy", "Tetris", "BadType")


class TestFetchCover:
    def _make_client(self, status: int, body: bytes) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=body)

        transport = httpx.MockTransport(handler)
        return httpx.Client(transport=transport)

    def test_fetch_writes_file_on_200(self, tmp_path: Path) -> None:
        client = self._make_client(200, b"\x89PNG\r\n\x1a\nDATA")
        result = libretro.fetch_cover(
            "Nintendo - Game Boy",
            "gb",
            "Tetris (World)",
            "Named_Boxarts",
            tmp_path,
            client=client,
        )
        assert result is not None
        local_path, source_url = result
        assert local_path.exists()
        assert local_path.read_bytes() == b"\x89PNG\r\n\x1a\nDATA"
        assert source_url.endswith("Tetris%20%28World%29.png")

    def test_fetch_returns_none_on_404(self, tmp_path: Path) -> None:
        client = self._make_client(404, b"")
        result = libretro.fetch_cover(
            "Nintendo - Game Boy",
            "gb",
            "Nonexistent",
            "Named_Boxarts",
            tmp_path,
            client=client,
        )
        assert result is None

    def test_fetch_returns_none_on_500(self, tmp_path: Path) -> None:
        client = self._make_client(500, b"oops")
        result = libretro.fetch_cover(
            "Nintendo - Game Boy",
            "gb",
            "Tetris (World)",
            "Named_Boxarts",
            tmp_path,
            client=client,
        )
        assert result is None

    def test_fetch_skips_when_cached(self, tmp_path: Path) -> None:
        # Pre-create the cached file; the client should never be invoked.
        cached = libretro.cover_cache_path(tmp_path, "gb", "Named_Boxarts", "Tetris (World)")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"already-here")

        def boom(request: httpx.Request) -> httpx.Response:
            raise AssertionError("client was called despite cache hit")

        client = httpx.Client(transport=httpx.MockTransport(boom))
        result = libretro.fetch_cover(
            "Nintendo - Game Boy",
            "gb",
            "Tetris (World)",
            "Named_Boxarts",
            tmp_path,
            client=client,
        )
        assert result is not None
        local_path, _ = result
        assert local_path.read_bytes() == b"already-here"

    def test_fetch_rejects_non_image_response(self, tmp_path: Path) -> None:
        """A 200 with HTML/JS body is rejected and not cached (audit #8)."""
        client = self._make_client(200, b"<html>compromised</html>")
        result = libretro.fetch_cover(
            "Nintendo - Game Boy",
            "gb",
            "Tetris (World)",
            "Named_Boxarts",
            tmp_path,
            client=client,
        )
        assert result is None
        dest = libretro.cover_cache_path(
            tmp_path, "gb", "Named_Boxarts", "Tetris (World)"
        )
        assert not dest.exists()

    def test_fetch_atomic_write_no_partial_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a write failure mid-rename; the destination must not exist
        # (so future runs treat the slot as "not cached" and retry cleanly).
        client = self._make_client(200, b"\x89PNG\r\n\x1a\nDATA")

        def _bad_replace(src: object, dst: object) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr("romulus.metadata.libretro.os.replace", _bad_replace)
        result = libretro.fetch_cover(
            "Nintendo - Game Boy",
            "gb",
            "Tetris (World)",
            "Named_Boxarts",
            tmp_path,
            client=client,
        )
        assert result is None
        dest = libretro.cover_cache_path(
            tmp_path, "gb", "Named_Boxarts", "Tetris (World)"
        )
        assert not dest.exists()
        # The .part temp file should also have been cleaned up.
        leftovers = list(dest.parent.glob("*.part"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# Hasheous
# ---------------------------------------------------------------------------


class TestParseHasheousResponse:
    def test_flat_payload(self) -> None:
        payload = {
            "title": "Super Mario World",
            "description": "Side-scrolling platformer.",
            "genre": "Platformer",
            "developer": "Nintendo EAD",
            "publisher": "Nintendo",
            "release_date": "1990-11-21",
        }
        parsed = hasheous.parse_hasheous_response(payload)
        assert parsed["title"] == "Super Mario World"
        assert parsed["description"].startswith("Side-scrolling")
        assert parsed["genre"] == "Platformer"
        assert parsed["release_date"] == "1990-11-21"

    def test_nested_under_game(self) -> None:
        payload = {"game": {"name": "Tetris", "summary": "Falling blocks."}}
        parsed = hasheous.parse_hasheous_response(payload)
        assert parsed["title"] == "Tetris"
        assert parsed["description"] == "Falling blocks."

    def test_empty_payload(self) -> None:
        parsed = hasheous.parse_hasheous_response({})
        assert parsed["title"] is None
        assert parsed["description"] is None


class TestLookupByHash:
    def _patch_no_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

    def test_lookup_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_no_rate_limit(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            assert "sha1" in request.url.path
            return httpx.Response(
                200,
                json={"title": "Tetris", "genre": "Puzzle"},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = hasheous.lookup_by_hash("ABCDEF" * 6 + "ABCD", client=client)
        assert result is not None
        assert result["title"] == "Tetris"
        assert result["genre"] == "Puzzle"

    def test_lookup_404_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_no_rate_limit(monkeypatch)

        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )
        assert hasheous.lookup_by_hash("0" * 40, client=client) is None

    def test_lookup_empty_hash_returns_none(self) -> None:
        assert hasheous.lookup_by_hash("", client=httpx.Client()) is None

    def test_lookup_429_backs_off_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_no_rate_limit(monkeypatch)
        monkeypatch.setattr(hasheous, "BACKOFF_BASE", 0.0)

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"title": "Zelda"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = hasheous.lookup_by_hash("a" * 40, client=client)
        assert result is not None
        assert result["title"] == "Zelda"
        assert len(attempts) == 2

    def test_lookup_network_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_no_rate_limit(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert hasheous.lookup_by_hash("a" * 40, client=client) is None

    def test_lookup_rejects_malformed_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense-in-depth: a non-hex hash never reaches the network."""
        self._patch_no_rate_limit(monkeypatch)
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        # Path-traversal attempt — must be rejected without an HTTP call.
        assert hasheous.lookup_by_hash("../etc/passwd", client=client) is None
        # Wrong length (39 chars) — also rejected.
        assert hasheous.lookup_by_hash("a" * 39, client=client) is None
        # Non-hex characters — rejected.
        assert hasheous.lookup_by_hash("g" * 40, client=client) is None
        assert calls == []


# ---------------------------------------------------------------------------
# LaunchBox XML
# ---------------------------------------------------------------------------


_LAUNCHBOX_XML = """<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
  <Game>
    <Name>Super Mario World</Name>
    <Platform>Super Nintendo Entertainment System</Platform>
    <Overview>Mario adventures in Dinosaur Land.</Overview>
    <Genres>Platformer</Genres>
    <Developer>Nintendo EAD</Developer>
    <Publisher>Nintendo</Publisher>
    <ReleaseDate>1990-11-21</ReleaseDate>
    <MaxPlayers>2</MaxPlayers>
    <ESRB>E</ESRB>
  </Game>
  <Game>
    <Name>The Legend of Zelda</Name>
    <Platform>Nintendo Entertainment System</Platform>
    <Overview>Hyrule awaits.</Overview>
    <Genres>Action-Adventure</Genres>
    <Developer>Nintendo R&amp;D4</Developer>
    <Publisher>Nintendo</Publisher>
    <ReleaseDate>1986-02-21</ReleaseDate>
  </Game>
  <Game>
    <Name></Name>
    <Platform>Unknown</Platform>
  </Game>
</LaunchBox>
"""


class TestLaunchBoxXml:
    def _write_xml(self, tmp_path: Path) -> Path:
        xml = tmp_path / "Metadata.xml"
        xml.write_text(_LAUNCHBOX_XML, encoding="utf-8")
        return xml

    def test_parse_extracts_games(self, tmp_path: Path) -> None:
        entries = launchbox.parse_launchbox_xml(self._write_xml(tmp_path))
        titles = [e.title for e in entries]
        assert "Super Mario World" in titles
        assert "The Legend of Zelda" in titles
        # Empty-name entry is skipped.
        assert "" not in titles
        smw = next(e for e in entries if e.title == "Super Mario World")
        assert smw.system_id == "snes"
        assert smw.developer == "Nintendo EAD"
        assert smw.players == "2"

    def test_match_by_title_and_system(self, tmp_path: Path) -> None:
        entries = launchbox.parse_launchbox_xml(self._write_xml(tmp_path))
        index = launchbox.build_index(entries)
        hit = launchbox.match_game("Super Mario World", "snes", index)
        assert hit is not None
        assert hit.developer == "Nintendo EAD"

    def test_match_is_tolerant_of_punctuation(self, tmp_path: Path) -> None:
        entries = launchbox.parse_launchbox_xml(self._write_xml(tmp_path))
        index = launchbox.build_index(entries)
        hit = launchbox.match_game("super-mario-world!", "snes", index)
        assert hit is not None

    def test_match_returns_none_for_unknown(self, tmp_path: Path) -> None:
        entries = launchbox.parse_launchbox_xml(self._write_xml(tmp_path))
        index = launchbox.build_index(entries)
        assert launchbox.match_game("Made-up Game", "snes", index) is None

    def test_billion_laughs_xml_is_rejected(self, tmp_path: Path) -> None:
        """defusedxml must refuse internal-entity expansion (audit #3)."""
        import pytest
        from defusedxml.common import DefusedXmlException

        bomb_xml = """<?xml version="1.0"?>
<!DOCTYPE root [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<LaunchBox>
    <Game>
        <Name>&lol3;</Name>
    </Game>
</LaunchBox>
"""
        bomb = tmp_path / "bomb.xml"
        bomb.write_text(bomb_xml, encoding="utf-8")
        # defusedxml raises rather than expanding — the LaunchBox parser does
        # not catch it (a real LaunchBox file is trustworthy enough that we
        # surface the parse error rather than silently producing an empty
        # index). What matters is that the entity is NEVER expanded into
        # ``lol*1000`` (or worse, the depth-6 billion).
        with pytest.raises(DefusedXmlException):
            launchbox.parse_launchbox_xml(bomb)

    def test_entry_to_metadata_shape(self, tmp_path: Path) -> None:
        entries = launchbox.parse_launchbox_xml(self._write_xml(tmp_path))
        smw = next(e for e in entries if e.title == "Super Mario World")
        meta = launchbox.entry_to_metadata(smw)
        assert set(meta.keys()) == {
            "description",
            "genre",
            "developer",
            "publisher",
            "release_date",
            "players",
            "rating",
        }
        assert meta["genre"] == "Platformer"


# ---------------------------------------------------------------------------
# ScreenScraper
# ---------------------------------------------------------------------------


class TestScreenScraper:
    def test_no_credentials_short_circuits(self) -> None:
        assert screenscraper.lookup_game("a" * 40, "snes", None) is None
        assert screenscraper.lookup_game("a" * 40, "snes", {"username": "", "password": ""}) is None

    def test_parse_jeu_infos(self) -> None:
        payload = {
            "response": {
                "jeu": {
                    "nom": "Sonic",
                    "synopsis": [{"langue": "en", "text": "Fast hedgehog."}],
                    "genres": [{"text": "Platformer"}],
                    "developpeur": "Sega",
                    "editeur": "Sega",
                    "joueurs": "1",
                }
            }
        }
        out = screenscraper.parse_screenscraper_response(payload)
        assert out is not None
        assert out["description"] == "Fast hedgehog."
        assert out["genre"] == "Platformer"
        assert out["developer"] == "Sega"
        assert out["players"] == "1"

    def test_parse_missing_jeu_returns_none(self) -> None:
        assert screenscraper.parse_screenscraper_response({}) is None
        assert screenscraper.parse_screenscraper_response({"response": {}}) is None

    def test_lookup_with_credentials_calls_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(screenscraper, "MIN_REQUEST_INTERVAL", 0.0)

        def handler(request: httpx.Request) -> httpx.Response:
            assert "sha1" in request.url.params
            return httpx.Response(
                200,
                json={"response": {"jeu": {"nom": "Test", "joueurs": "1"}}},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = screenscraper.lookup_game(
            "a" * 40,
            "snes",
            {"username": "u", "password": "p"},
            client=client,
        )
        assert result is not None
        assert result["title"] == "Test"


class TestScreenScraperTestConnection:
    """Cover the Settings dialog's `Test connection` button entry point."""

    def test_empty_credentials_short_circuits(self) -> None:
        ok, msg = screenscraper.test_connection("", "")
        assert ok is False
        assert "username" in msg.lower()

    def test_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/ssuserInfos.php")
            assert request.url.params["ssid"] == "alice"
            assert request.url.params["sspassword"] == "secret"
            return httpx.Response(
                200,
                json={"response": {"ssuser": {"id": "1", "maxthreads": "1"}}},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok, msg = screenscraper.test_connection("alice", "secret", client=client)
        assert ok is True
        assert "successful" in msg.lower()

    def test_invalid_credentials_returns_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad credentials")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok, msg = screenscraper.test_connection("alice", "wrong", client=client)
        assert ok is False
        assert "invalid" in msg.lower()

    def test_non_json_body_returns_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok, msg = screenscraper.test_connection("alice", "secret", client=client)
        assert ok is False
        assert "non-json" in msg.lower()

    def test_unexpected_status_returns_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="service unavailable")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok, msg = screenscraper.test_connection("alice", "secret", client=client)
        assert ok is False
        assert "503" in msg

    def test_network_error_returns_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS failure")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok, msg = screenscraper.test_connection("alice", "secret", client=client)
        assert ok is False
        assert "network error" in msg.lower()


# ---------------------------------------------------------------------------
# Queries: metadata + covers
# ---------------------------------------------------------------------------


class TestMetadataQueries:
    def test_upsert_and_get_metadata(self, seeded_db) -> None:
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Tetris.gb",
                "filename": "Tetris.gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Tetris",
                "match_confidence": "dat_verified",
            },
        )
        q.upsert_metadata(
            seeded_db,
            rom_id,
            {"description": "Falling blocks.", "genre": "Puzzle"},
            source="hasheous",
        )
        row = q.get_metadata(seeded_db, rom_id)
        assert row is not None
        assert row["description"] == "Falling blocks."
        assert row["genre"] == "Puzzle"
        assert row["source"] == "hasheous"

    def test_upsert_replaces_existing(self, seeded_db) -> None:
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Tetris2.gb",
                "filename": "Tetris2.gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Tetris 2",
                "match_confidence": "dat_verified",
            },
        )
        q.upsert_metadata(seeded_db, rom_id, {"description": "v1"}, source="hasheous")
        q.upsert_metadata(seeded_db, rom_id, {"description": "v2"}, source="launchbox")
        row = q.get_metadata(seeded_db, rom_id)
        assert row["description"] == "v2"
        assert row["source"] == "launchbox"

    def test_insert_and_get_covers(self, seeded_db) -> None:
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Tetris3.gb",
                "filename": "Tetris3.gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Tetris 3",
                "match_confidence": "dat_verified",
            },
        )
        q.insert_cover(
            seeded_db,
            rom_id,
            "Named_Boxarts",
            "https://example.com/Tetris.png",
            "/tmp/Tetris.png",
        )
        covers = q.get_covers(seeded_db, rom_id)
        assert len(covers) == 1
        assert covers[0]["cover_type"] == "Named_Boxarts"
        assert q.has_cover(seeded_db, rom_id, "Named_Boxarts") is True
        assert q.has_cover(seeded_db, rom_id, "Named_Snaps") is False

    def test_get_roms_needing_enrichment(self, seeded_db) -> None:
        # Verified ROM with no metadata -> appears.
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/TetrisQ.gb",
                "filename": "TetrisQ.gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Tetris",
                "canonical_name": "Tetris (World)",
                "dat_match": "Tetris (World)",
                "match_confidence": "dat_verified",
            },
        )
        seeded_db.commit()

        rows = q.get_roms_needing_enrichment(seeded_db)
        assert len(rows) == 1
        assert rows[0]["title"] == "Tetris"

        # Now add metadata — it should disappear from the queue.
        q.upsert_metadata(seeded_db, rom_id, {"description": "x"}, source="hasheous")
        rows = q.get_roms_needing_enrichment(seeded_db)
        assert rows == []

    def test_unverified_roms_are_excluded(self, seeded_db) -> None:
        q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Mystery.gb",
                "filename": "Mystery.gb",
                "extension": ".gb",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Mystery",
                "match_confidence": "fuzzy",
            },
        )
        seeded_db.commit()
        assert q.get_roms_needing_enrichment(seeded_db) == []

    def test_include_fuzzy_surfaces_fuzzy_matches(self, seeded_db) -> None:
        """include_fuzzy=True must drop the dat_verified filter."""
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/MysteryF.gb",
                "filename": "MysteryF.gb",
                "extension": ".gb",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Mystery Fuzzy",
                "match_confidence": "fuzzy",
            },
        )
        seeded_db.commit()

        # Default: excluded.
        assert q.get_roms_needing_enrichment(seeded_db) == []
        # Loosened: surfaced.
        rows = q.get_roms_needing_enrichment(seeded_db, include_fuzzy=True)
        assert len(rows) == 1
        assert rows[0]["id"] == rom_id

    def test_include_already_enriched_keeps_metadata_rows(self, seeded_db) -> None:
        """include_already_enriched=True must drop the m.rom_id IS NULL filter."""
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/TetrisE.gb",
                "filename": "TetrisE.gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Tetris Enriched",
                "canonical_name": "Tetris Enriched",
                "dat_match": "Tetris Enriched",
                "match_confidence": "dat_verified",
            },
        )
        q.upsert_metadata(seeded_db, rom_id, {"description": "x"}, source="hasheous")
        seeded_db.commit()

        # Default: excluded (already has metadata).
        assert q.get_roms_needing_enrichment(seeded_db) == []
        # Loosened: surfaced for a re-run.
        rows = q.get_roms_needing_enrichment(seeded_db, include_already_enriched=True)
        assert len(rows) == 1
        assert rows[0]["id"] == rom_id

    def test_both_flags_combine_multiplicatively(self, seeded_db) -> None:
        """Both flags True must return fuzzy AND already-enriched rows."""
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Forced.gb",
                "filename": "Forced.gb",
                "extension": ".gb",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Forced",
                "match_confidence": "fuzzy",
            },
        )
        q.upsert_metadata(seeded_db, rom_id, {"description": "x"}, source="hasheous")
        seeded_db.commit()

        # Either flag alone leaves it filtered out.
        assert q.get_roms_needing_enrichment(seeded_db) == []
        assert q.get_roms_needing_enrichment(seeded_db, include_fuzzy=True) == []
        assert q.get_roms_needing_enrichment(seeded_db, include_already_enriched=True) == []
        # Both together: surfaced.
        rows = q.get_roms_needing_enrichment(
            seeded_db,
            include_fuzzy=True,
            include_already_enriched=True,
        )
        assert len(rows) == 1
        assert rows[0]["id"] == rom_id


# ---------------------------------------------------------------------------
# enrich_library orchestrator
# ---------------------------------------------------------------------------


def _seed_verified_game(db, *, title: str, system_id: str, sha1: str | None) -> int:
    """Insert a verified ROM and return its rom_id.

    The Session-15 schema is strict 1:1 rom→game — there is no ``games`` table
    and no ``link_rom_to_game``. A "verified game" is just a rom row with
    ``match_confidence='dat_verified'`` and a populated ``canonical_name``.

    ``fuzzy_key`` is derived from the title so the ROM appears in queries that
    require ``fuzzy_key IS NOT NULL`` (e.g. ``fetch_online_covers_for_scope``).
    """
    # Simple fuzzy_key: lowercase alphanumeric only.
    fuzzy_key = "".join(c for c in title.lower() if c.isalnum())
    rom_id = q.upsert_rom(
        db,
        {
            "path": f"/lib/{system_id}/{title}.bin",
            "filename": f"{title}.bin",
            "extension": ".bin",
            "size_bytes": 1024,
            "mtime": time.time(),
            "system_id": system_id,
            "title": title,
            "canonical_name": title,
            "dat_match": title,
            "fuzzy_key": fuzzy_key,
            "match_confidence": "dat_verified",
        },
    )
    if sha1:
        q.upsert_hash(db, rom_id, crc32="ffffffff", sha1=sha1, md5=None)
    db.commit()
    return rom_id


class TestEnrichLibrary:
    def test_enrich_writes_metadata_only(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post metadata/covers split: enrich_library no longer fetches covers."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        sha1 = "a" * 40
        rom_id = _seed_verified_game(
            seeded_db, title="Super Mario World", system_id="snes", sha1=sha1
        )

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(
                    200,
                    json={
                        "title": "Super Mario World",
                        "description": "Mario in Dinosaur Land.",
                        "genre": "Platformer",
                    },
                )
            if "thumbnails.libretro.com" in host:
                # Enrich must NOT touch the cover CDN any more — split
                # into the Find Covers worker. Fail loudly if it does.
                raise AssertionError(
                    "enrich_library should not fetch covers post-split"
                )
            raise AssertionError(f"unexpected host: {host}")

        client = httpx.Client(transport=httpx.MockTransport(handler))

        progress_events: list[tuple[int, int, str]] = []
        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            progress_callback=lambda i, t, name: progress_events.append((i, t, name)),
            http_client=client,
        )

        assert stats["games_processed"] == 1
        assert stats["metadata_added"] == 1
        # Stats field is still emitted (back-compat) but always 0 now.
        assert stats["covers_added"] == 0
        assert progress_events == [(1, 1, "Super Mario World")]

        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["description"] == "Mario in Dinosaur Land."
        assert meta["source"] == "hasheous"

        # No covers should have been inserted — enrich is metadata-only now.
        assert q.get_covers(seeded_db, rom_id) == []

    def test_enrich_falls_back_to_launchbox(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        rom_id = _seed_verified_game(
            seeded_db, title="Super Mario World", system_id="snes", sha1="b" * 40
        )

        # Hasheous misses; libretro 404s.
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(404)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        xml_path = tmp_path / "Metadata.xml"
        xml_path.write_text(_LAUNCHBOX_XML, encoding="utf-8")

        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            launchbox_xml_path=xml_path,
            http_client=client,
        )

        assert stats["metadata_added"] == 1
        assert stats["covers_added"] == 0
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "launchbox"
        assert meta["developer"] == "Nintendo EAD"

    def test_enrich_skips_already_enriched(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        rom_id = _seed_verified_game(
            seeded_db, title="Tetris", system_id="gb", sha1="c" * 40
        )
        q.upsert_metadata(
            seeded_db, rom_id, {"description": "already here"}, source="manual"
        )
        seeded_db.commit()

        def boom(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network was contacted despite cache")

        client = httpx.Client(transport=httpx.MockTransport(boom))
        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
        )
        # No metadata work to do; libretro lookups also don't fire because we
        # have nothing in the needs-enrichment queue.
        assert stats["games_processed"] == 0

    def test_enrich_handles_no_credentials_for_screenscraper(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All providers miss; ScreenScraper has no credentials. Must not raise.
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        _seed_verified_game(
            seeded_db, title="Unknown", system_id="gb", sha1="d" * 40
        )

        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )
        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
        )
        assert stats["games_processed"] == 1
        assert stats["metadata_added"] == 0
        assert stats["covers_added"] == 0

    def test_include_fuzzy_flag_processes_fuzzy_games(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enrich_library must honour the include_fuzzy flag end-to-end."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        # In the 1:1 schema there is no games table — a fuzzy-confidence ROM
        # stands on its own.
        q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Fuzzy.gb",
                "filename": "Fuzzy.gb",
                "extension": ".gb",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Fuzzy",
                "match_confidence": "fuzzy",
            },
        )
        seeded_db.commit()

        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )

        # Without the flag: zero processed (filter excludes fuzzy).
        stats = enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )
        assert stats["games_processed"] == 0

        # With the flag: the fuzzy ROM is processed (still no metadata
        # found because every provider 404s, but the worker reached it).
        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
            include_fuzzy=True,
        )
        assert stats["games_processed"] == 1

    def test_force_path_re_enriches_existing(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both flags True + scoped rom_ids must re-run an already-enriched ROM."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        rom_id = _seed_verified_game(
            seeded_db, title="Tetris", system_id="gb", sha1="f" * 40
        )
        # Seed a stale metadata row from a prior enrich.
        q.upsert_metadata(
            seeded_db, rom_id, {"description": "stale"}, source="manual"
        )
        seeded_db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(
                    200,
                    json={"title": "Tetris", "description": "fresh"},
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
            rom_ids=[rom_id],
            include_fuzzy=True,
            include_already_enriched=True,
        )
        assert stats["games_processed"] == 1
        assert stats["metadata_added"] == 1
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["description"] == "fresh"


# Sanity: confirm every network-touching metadata function refuses to reach
# the real internet when the caller supplies a MockTransport-backed client.
# If a future refactor accidentally bypasses the injected client (e.g. by
# building its own httpx.Client mid-function), the mock transport's "raises
# on any request" guard below will fire and this test will fail loudly.
def test_module_does_not_smuggle_real_network_calls() -> None:
    def _raise(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("real network call attempted in test")

    guarded = httpx.Client(transport=httpx.MockTransport(_raise))
    try:
        # 404 / non-200 paths short-circuit before reading body — but the
        # request itself goes through the transport, so MockTransport sees it.
        with pytest.raises(AssertionError):
            libretro.fetch_cover(
                "Nintendo - Test", "test", "Game", "Named_Boxarts", "/tmp", client=guarded
            )
        with pytest.raises(AssertionError):
            hasheous.lookup_by_hash(
                "a" * 40, client=guarded, rate_limit=False
            )
        with pytest.raises(AssertionError):
            screenscraper.lookup_game(
                "a" * 40,
                "snes",
                {"username": "u", "password": "p"},
                client=guarded,
                rate_limit=False,
            )
    finally:
        guarded.close()


# ---------------------------------------------------------------------------
# TheGamesDB client
# ---------------------------------------------------------------------------


def _tgdb_envelope(games: list[dict], remaining: int | None = 999) -> dict:
    """Build a minimal TGDB ByGameName envelope around a games list."""
    return {
        "code": 200,
        "status": "Success",
        "data": {"count": len(games), "games": games},
        "remaining_monthly_allowance": remaining,
    }


class TestTgdbParseResponse:
    def test_picks_exact_title_match(self) -> None:
        envelope = _tgdb_envelope([
            {"id": 1, "game_title": "Other Game", "overview": "wrong"},
            {
                "id": 2,
                "game_title": "Super Mario World",
                "overview": "Right one.",
                "developers": [1],
                "publishers": [2, 3],
                "release_date": "1990-11-21",
            },
        ])
        parsed = thegamesdb.parse_response(envelope, "Super Mario World")
        assert parsed is not None
        assert parsed["description"] == "Right one."
        assert parsed["release_date"] == "1990-11-21"
        assert parsed["developer"] == "1"
        assert parsed["publisher"] == "2, 3"

    def test_normalises_punctuation_in_title(self) -> None:
        """``Mario's`` should match ``Marios`` after normalisation."""
        envelope = _tgdb_envelope([
            {"id": 1, "game_title": "Marios Picross"},
        ])
        parsed = thegamesdb.parse_response(envelope, "Mario's Picross")
        assert parsed is not None

    def test_no_match_returns_none(self) -> None:
        envelope = _tgdb_envelope([
            {"id": 1, "game_title": "Completely Unrelated"},
        ])
        assert thegamesdb.parse_response(envelope, "Tetris") is None

    def test_malformed_envelope_returns_none(self) -> None:
        assert thegamesdb.parse_response({}, "Tetris") is None
        assert thegamesdb.parse_response({"data": "not-a-dict"}, "Tetris") is None
        assert thegamesdb.parse_response({"data": {"games": "x"}}, "Tetris") is None

    def test_strips_no_intro_tags_before_matching(self) -> None:
        """``Super Mario World (USA)`` must match TGDB's ``Super Mario World``."""
        envelope = _tgdb_envelope([
            {"id": 1, "game_title": "Super Mario World", "overview": "hit"},
        ])
        parsed = thegamesdb.parse_response(envelope, "Super Mario World (USA)")
        assert parsed is not None
        assert parsed["description"] == "hit"

    def test_substring_fallback_matches_series_prefix(self) -> None:
        """Query without series prefix should match TGDB's full title."""
        envelope = _tgdb_envelope([
            {
                "id": 25838,
                "game_title": "James Bond 007 - Everything or Nothing",
                "overview": "Bond, but tiny.",
            },
        ])
        parsed = thegamesdb.parse_response(
            envelope, "007 - Everything or Nothing"
        )
        assert parsed is not None
        assert parsed["description"] == "Bond, but tiny."

    def test_substring_fallback_rejects_short_titles(self) -> None:
        """Short titles must NOT substring-match to avoid false positives."""
        # ``Tetris`` (6 normalised chars) is below _SUBSTRING_FALLBACK_MIN_LEN
        # so it must NOT match candidates that merely contain "tetris".
        envelope = _tgdb_envelope([
            {"id": 1, "game_title": "The New Tetris", "overview": "wrong"},
            {"id": 2, "game_title": "Tetris Plus", "overview": "also wrong"},
        ])
        assert thegamesdb.parse_response(envelope, "Tetris") is None

    def test_resolves_genre_ids_via_include_block(self) -> None:
        """When include.genres carries an id->name table, store the name."""
        envelope = {
            "code": 200,
            "data": {
                "count": 1,
                "games": [
                    {
                        "id": 1,
                        "game_title": "Super Mario World",
                        "genres": [8, 12],
                        "developers": [6037],
                        "publishers": [6037],
                    }
                ],
                "include": {
                    "genres": {
                        "data": {
                            "8": {"id": 8, "name": "Platformer"},
                            "12": {"id": 12, "name": "Adventure"},
                        }
                    },
                    "developers": {
                        "data": {"6037": {"id": 6037, "name": "Nintendo EAD"}}
                    },
                    "publishers": {
                        "data": {"6037": {"id": 6037, "name": "Nintendo"}}
                    },
                },
            },
            "remaining_monthly_allowance": 999,
        }
        parsed = thegamesdb.parse_response(envelope, "Super Mario World")
        assert parsed is not None
        assert parsed["genre"] == "Platformer, Adventure"
        assert parsed["developer"] == "Nintendo EAD"
        assert parsed["publisher"] == "Nintendo"

    def test_falls_back_to_raw_ids_when_no_include_block(self) -> None:
        """Without an include block, genres/devs/pubs keep raw id strings."""
        envelope = _tgdb_envelope([
            {
                "id": 1,
                "game_title": "Super Mario World",
                "genres": [8, 12],
            },
        ])
        parsed = thegamesdb.parse_response(envelope, "Super Mario World")
        assert parsed is not None
        # No lookup table -> ids stringified, joined with ", ".
        assert parsed["genre"] == "8, 12"


class TestTgdbLookupGame:
    def _patch_no_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(thegamesdb, "MIN_REQUEST_INTERVAL", 0.0)

    def test_no_apikey_skips_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_no_rate_limit(monkeypatch)
        calls: list[int] = []

        def handler(_req: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        payload, remaining = thegamesdb.lookup_game(
            "Tetris", "gb", apikey="", client=client
        )
        assert payload is None
        assert remaining is None
        assert calls == []

    def test_unmapped_platform_skips_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """j2me has no TGDB platform mapping — must short-circuit."""
        self._patch_no_rate_limit(monkeypatch)
        calls: list[int] = []

        def handler(_req: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        payload, remaining = thegamesdb.lookup_game(
            "Some Title", "j2me", apikey="KEY", client=client
        )
        assert payload is None
        assert remaining is None
        assert calls == []

    def test_returns_payload_and_remaining(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_no_rate_limit(monkeypatch)

        def handler(req: httpx.Request) -> httpx.Response:
            # apikey/platform filter must be present in the query string.
            assert req.url.params.get("apikey") == "KEY"
            assert req.url.params.get("filter[platform]") == "6"  # snes
            return httpx.Response(
                200,
                json=_tgdb_envelope(
                    [{"id": 1, "game_title": "Super Mario World",
                      "overview": "ok"}],
                    remaining=42,
                ),
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        payload, remaining = thegamesdb.lookup_game(
            "Super Mario World", "snes", apikey="KEY", client=client
        )
        assert payload is not None
        assert payload["description"] == "ok"
        assert remaining == 42

    def test_403_returns_zero_remaining(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 403 typically means apikey invalid or quota exhausted."""
        self._patch_no_rate_limit(monkeypatch)
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(403))
        )
        payload, remaining = thegamesdb.lookup_game(
            "Tetris", "gb", apikey="KEY", client=client
        )
        assert payload is None
        assert remaining == 0


class TestTgdbEnrichmentChain:
    def test_tgdb_fires_when_other_sources_miss(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        set_config(seeded_db, "thegamesdb_api_key", "KEY")
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(thegamesdb, "MIN_REQUEST_INTERVAL", 0.0)

        rom_id = _seed_verified_game(
            seeded_db, title="Super Mario World", system_id="snes", sha1="c" * 40
        )

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(404)  # miss -> falls through
            if "thegamesdb" in host:
                return httpx.Response(
                    200,
                    json=_tgdb_envelope(
                        [{"id": 99, "game_title": "Super Mario World",
                          "overview": "Mario via TGDB"}],
                        remaining=99,
                    ),
                )
            # libretro misses for now (this test isn't about covers).
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )

        assert stats["metadata_added"] == 1
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "thegamesdb"
        assert meta["description"] == "Mario via TGDB"
        # Allowance is persisted for the next session.
        assert get_config(seeded_db, "thegamesdb_remaining_allowance") == "99"

    def test_tgdb_skipped_when_hasheous_hits(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hash-keyed Hasheous wins; TGDB must NOT spend quota."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        set_config(seeded_db, "thegamesdb_api_key", "KEY")
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(thegamesdb, "MIN_REQUEST_INTERVAL", 0.0)
        _seed_verified_game(
            seeded_db, title="Tetris", system_id="gb", sha1="d" * 40
        )

        tgdb_calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(200, json={"title": "Tetris", "description": "Blocks."})
            if "thegamesdb" in host:
                tgdb_calls.append(1)
                return httpx.Response(200, json=_tgdb_envelope([]))
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )

        assert stats["metadata_added"] == 1
        assert tgdb_calls == [], "TGDB should not be called when Hasheous matches"

    def test_tgdb_disabled_after_zero_allowance(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once TGDB reports remaining=0, subsequent games skip it entirely."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        set_config(seeded_db, "thegamesdb_api_key", "KEY")
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(thegamesdb, "MIN_REQUEST_INTERVAL", 0.0)

        # Two games; both with hasheous misses so they'd both reach TGDB.
        _seed_verified_game(
            seeded_db, title="One", system_id="gb", sha1="1" * 40
        )
        _seed_verified_game(
            seeded_db, title="Two", system_id="gb", sha1="2" * 40
        )

        tgdb_calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(404)
            if "thegamesdb" in host:
                tgdb_calls.append(1)
                # First call returns remaining=0 so we should be disabled
                # from that point onwards.
                return httpx.Response(
                    200, json=_tgdb_envelope([], remaining=0)
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )
        assert len(tgdb_calls) == 1, (
            f"TGDB should disable after first remaining=0; got {len(tgdb_calls)} calls"
        )

    def test_tgdb_short_circuits_when_persisted_allowance_zero(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config carrying remaining=0 must skip TGDB without trying once."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        set_config(seeded_db, "thegamesdb_api_key", "KEY")
        set_config(seeded_db, "thegamesdb_remaining_allowance", "0")
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(thegamesdb, "MIN_REQUEST_INTERVAL", 0.0)

        _seed_verified_game(
            seeded_db, title="Game", system_id="gb", sha1="e" * 40
        )

        tgdb_calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(404)
            if "thegamesdb" in host:
                tgdb_calls.append(1)
                return httpx.Response(200, json=_tgdb_envelope([]))
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )
        assert tgdb_calls == [], "Persisted zero allowance must skip TGDB"


# ---------------------------------------------------------------------------
# GameDB client (offline, bundled JSON)
# ---------------------------------------------------------------------------


_GAMEDB_SAMPLE: dict[str, dict] = {
    "NUS-CDZJ-JPN": {
        "crc32": "0x9e978488",
        "crc32_128kb": "0xb58305ba",
        "developer": "Athena",
        "publisher": "Athena",
        "region": "NTSC-J",
        "release_date": "1998-06-26",
        "release_name": "Dezaemon 3D (Japan)",
        "serial": "NUS-CDZJ-JPN",
        "title": "Dezaemon 3D",
    },
    "NUS-NSME-USA": {
        "crc32": "0xbb95e7d5",
        "region": "NTSC-U",
        "release_name": "Super Mario 64 (USA)",
        "title": "Super Mario 64",
    },
}


def _build_gamedb_index() -> gamedb.GameDBIndex:
    """Build a GameDBIndex from the in-test sample dict."""
    return gamedb.GameDBIndex(list(_GAMEDB_SAMPLE.values()))


class TestGameDBNormalisation:
    def test_normalise_crc32_strips_prefix_and_lowercases(self) -> None:
        assert gamedb._normalise_crc32("0x9E978488") == "9e978488"
        assert gamedb._normalise_crc32("9e978488") == "9e978488"
        assert gamedb._normalise_crc32("0X9E978488") == "9e978488"

    def test_normalise_crc32_pads_short_values(self) -> None:
        assert gamedb._normalise_crc32("0xabc") == "00000abc"

    def test_normalise_crc32_rejects_non_hex(self) -> None:
        assert gamedb._normalise_crc32("0xZZZ") is None
        assert gamedb._normalise_crc32(None) is None
        assert gamedb._normalise_crc32("") is None

    def test_extract_year_from_full_date(self) -> None:
        assert gamedb._extract_year("1998-06-26") == 1998

    def test_extract_year_from_year_only(self) -> None:
        assert gamedb._extract_year("1998") == 1998

    def test_extract_year_rejects_garbage(self) -> None:
        assert gamedb._extract_year("not a date") is None
        assert gamedb._extract_year(None) is None
        assert gamedb._extract_year("3500-01-01") is None  # out of range


class TestGameDBIndex:
    def test_lookup_by_crc32_matches(self) -> None:
        index = _build_gamedb_index()
        entry = gamedb.lookup_by_crc32("9e978488", index)
        assert entry is not None
        assert entry["title"] == "Dezaemon 3D"

    def test_lookup_by_crc32_accepts_dat_form(self) -> None:
        """ROMulus's hashes.crc32 lacks the ``0x`` prefix that GameDB stores."""
        index = _build_gamedb_index()
        entry = gamedb.lookup_by_crc32("bb95e7d5", index)
        assert entry is not None
        assert entry["title"] == "Super Mario 64"

    def test_lookup_by_title_normalises(self) -> None:
        index = _build_gamedb_index()
        # Region tag should be stripped before comparison.
        entry = gamedb.lookup_by_title("Super Mario 64 (USA)", index)
        assert entry is not None
        assert entry["title"] == "Super Mario 64"

    def test_lookup_by_crc32_returns_none_on_miss(self) -> None:
        index = _build_gamedb_index()
        assert gamedb.lookup_by_crc32("deadbeef", index) is None

    def test_entry_to_metadata_extracts_year_from_full_date(self) -> None:
        entry = _GAMEDB_SAMPLE["NUS-CDZJ-JPN"]
        payload = gamedb.entry_to_metadata(entry)
        assert payload["publisher"] == "Athena"
        assert payload["release_date"] == "1998-06-26"
        assert payload["release_year"] == 1998

    def test_entry_to_metadata_omits_missing_fields(self) -> None:
        """Identifier-only entries (no publisher/date) produce a sparse payload."""
        entry = _GAMEDB_SAMPLE["NUS-NSME-USA"]
        payload = gamedb.entry_to_metadata(entry)
        assert payload.get("publisher") is None
        assert payload.get("release_date") is None
        assert payload.get("release_year") is None


class TestGameDBLoadIndex:
    def test_load_index_round_trips_a_real_file(self, tmp_path: Path) -> None:
        import json

        path = tmp_path / "fake.json"
        path.write_text(json.dumps(_GAMEDB_SAMPLE), encoding="utf-8")
        index = gamedb.load_index(path)
        assert index is not None
        assert index.entry_count == 2
        assert gamedb.lookup_by_crc32("9e978488", index) is not None

    def test_load_index_returns_none_on_malformed_file(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        assert gamedb.load_index(path) is None

    def test_load_index_returns_none_on_top_level_array(self, tmp_path: Path) -> None:
        """GameDB files must be dicts of serial -> entry; arrays are unsupported."""
        import json

        path = tmp_path / "list.json"
        path.write_text(json.dumps([{"title": "x"}]), encoding="utf-8")
        assert gamedb.load_index(path) is None


class TestGameDBEnrichmentChain:
    """Tests asserting GameDB behaves correctly when libretro doesn't match.

    Each test patches ``libretro_metadat.get_index_for_system`` to return
    ``None`` so the chain falls through to GameDB unambiguously. Real
    libretro-database bundled files would otherwise win for many CRC32s.
    """

    def _disable_libretro(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            libretro_metadat, "get_index_for_system", lambda _sid: None
        )

    def test_gamedb_fires_first_when_crc32_matches(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GameDB hits short-circuit the chain before any HTTP call fires."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        gamedb.reset_cache_for_tests()
        self._disable_libretro(monkeypatch)

        rom_id = _seed_verified_game(
            seeded_db, title="Dezaemon 3D", system_id="n64", sha1="a" * 40
        )
        # Inject a CRC32 on the hash row so GameDB has something to match.
        q.upsert_hash(
            seeded_db,
            rom_id,
            crc32="9e978488",
            sha1="a" * 40,
            md5=None,
        )
        seeded_db.commit()

        # Point the GameDB resolver at our test fixture by injecting a
        # patched ``get_index_for_system`` — much simpler than staging a
        # full data/gamedb/n64.json file in tmp_path.
        index = _build_gamedb_index()
        monkeypatch.setattr(
            gamedb, "get_index_for_system", lambda sid: index if sid == "n64" else None
        )

        # Cover lookups still hit libretro-thumbnails — that's a separate
        # concern from metadata. We assert only that no *metadata* provider
        # was contacted, by counting calls and excluding the libretro CDN.
        non_cover_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "thumbnails.libretro.com" not in request.url.host:
                non_cover_calls.append(request.url.host)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )

        assert stats["metadata_added"] == 1
        assert non_cover_calls == [], (
            f"Expected only libretro cover calls; got: {non_cover_calls}"
        )
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "gamedb"
        assert meta["publisher"] == "Athena"
        assert meta["release_date"] == "1998-06-26"
        assert meta["release_year"] == 1998

    def test_gamedb_skips_when_no_user_facing_fields(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identifier-only GameDB entries must fall through to later providers.

        Otherwise we'd commit a metadata row with no description/dev/pub/
        date and the game would be locked out of richer providers on
        subsequent enrich passes.
        """
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        gamedb.reset_cache_for_tests()
        self._disable_libretro(monkeypatch)

        rom_id = _seed_verified_game(
            seeded_db, title="Super Mario 64", system_id="n64", sha1="b" * 40
        )
        q.upsert_hash(
            seeded_db, rom_id, crc32="bb95e7d5", sha1="b" * 40, md5=None
        )
        seeded_db.commit()

        index = _build_gamedb_index()
        monkeypatch.setattr(
            gamedb, "get_index_for_system", lambda sid: index if sid == "n64" else None
        )

        # Hasheous returns a hit so we can verify the chain progressed.
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                return httpx.Response(
                    200,
                    json={"title": "Super Mario 64", "description": "Bowser."},
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )
        assert stats["metadata_added"] == 1
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        # GameDB had only release_name/title (no publisher/date), so chain
        # progressed to Hasheous which DID return user-facing data.
        assert meta["source"] == "hasheous"
        assert meta["description"] == "Bowser."

    def test_gamedb_falls_back_to_title_when_crc_misses(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A quick-scan-only game (no CRC) should still benefit from title match."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        gamedb.reset_cache_for_tests()
        self._disable_libretro(monkeypatch)

        # Use a verified ROM with no CRC (sha1 only for identity).
        rom_id = _seed_verified_game(
            seeded_db, title="Dezaemon 3D", system_id="n64", sha1="c" * 40
        )
        # No CRC32 written — sha1 only.
        seeded_db.commit()

        index = _build_gamedb_index()
        monkeypatch.setattr(
            gamedb, "get_index_for_system", lambda sid: index if sid == "n64" else None
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "gamedb"
        assert meta["release_year"] == 1998


# ---------------------------------------------------------------------------
# libretro-database metadat client
# ---------------------------------------------------------------------------


_LIBRETRO_GENRE_DAT = """\
clrmamepro (
\tname "Test System"
\tdescription "Test System"
)

game (
\tcomment "Game One (USA)"
\tgenre "Shooter"
\trom ( crc 56C83C16 )
)

game (
\tcomment "Game Two (Japan)"
\tgenre "Sports"
\trom ( crc C8CDB4ED )
)
"""

_LIBRETRO_DEVELOPER_DAT = """\
clrmamepro (
\tname "Test System"
)

game (
\tcomment "Game One (USA)"
\tdeveloper "Studio X"
\trom ( crc 56C83C16 )
)
"""

_LIBRETRO_PUBLISHER_DAT = """\
clrmamepro (
\tname "Test System"
)

game (
\tcomment "Game One (USA)"
\tpublisher "Big Pub"
\trom ( crc 56C83C16 )
)
"""

_LIBRETRO_RELEASEYEAR_DAT = """\
clrmamepro (
\tname "Test System"
)

game (
\tcomment "Game One (USA)"
\treleaseyear "2003"
\trom ( crc 56C83C16 )
)
"""

_LIBRETRO_MAXUSERS_DAT = """\
clrmamepro (
\tname "Test System"
)

game (
\tcomment "Game One (USA)"
\tmaxusers "2"
\trom ( crc 56C83C16 )
)
"""


def _stage_libretro_metadat(root: Path, libretro_name: str) -> None:
    """Write fixture dat files under root/data/libretro-metadat/<dim>/."""
    base = root / "data" / "libretro-metadat"
    fixtures = {
        "genre": _LIBRETRO_GENRE_DAT,
        "developer": _LIBRETRO_DEVELOPER_DAT,
        "publisher": _LIBRETRO_PUBLISHER_DAT,
        "releaseyear": _LIBRETRO_RELEASEYEAR_DAT,
        "maxusers": _LIBRETRO_MAXUSERS_DAT,
    }
    for dim, content in fixtures.items():
        (base / dim).mkdir(parents=True, exist_ok=True)
        (base / dim / f"{libretro_name}.dat").write_text(
            content, encoding="utf-8"
        )


class TestLibretroMetadatParser:
    def test_parse_genre_file(self, tmp_path: Path) -> None:
        path = tmp_path / "g.dat"
        path.write_text(_LIBRETRO_GENRE_DAT, encoding="utf-8")
        table = libretro_metadat.parse_metadat_file(path, "genre")
        assert table == {
            "56c83c16": "Shooter",
            "c8cdb4ed": "Sports",
        }

    def test_parse_ignores_blocks_without_crc(self, tmp_path: Path) -> None:
        bad = "game (\n\tcomment \"No CRC\"\n\tgenre \"Action\"\n)\n"
        path = tmp_path / "g.dat"
        path.write_text(bad, encoding="utf-8")
        assert libretro_metadat.parse_metadat_file(path, "genre") == {}

    def test_parse_ignores_blocks_without_dimension(self, tmp_path: Path) -> None:
        bad = (
            "game (\n\tcomment \"Has CRC but no genre\"\n"
            "\trom ( crc 12345678 )\n)\n"
        )
        path = tmp_path / "g.dat"
        path.write_text(bad, encoding="utf-8")
        assert libretro_metadat.parse_metadat_file(path, "genre") == {}


class TestLibretroMetadatIndex:
    def test_lookup_merges_across_dimensions(self) -> None:
        idx = libretro_metadat.LibretroMetadatIndex({
            "genre": {"56c83c16": "Shooter"},
            "developer": {"56c83c16": "Studio X"},
            "releaseyear": {"56c83c16": "2003"},
        })
        merged = idx.lookup("56c83c16")
        assert merged == {
            "genre": "Shooter",
            "developer": "Studio X",
            "releaseyear": "2003",
        }

    def test_lookup_returns_empty_for_unknown_crc(self) -> None:
        idx = libretro_metadat.LibretroMetadatIndex({
            "genre": {"56c83c16": "Shooter"},
        })
        assert idx.lookup("deadbeef") == {}

    def test_entry_to_metadata_maps_dimensions_to_payload_keys(self) -> None:
        entry = {
            "genre": "Shooter",
            "developer": "Studio X",
            "publisher": "Big Pub",
            "releaseyear": "2003",
            "maxusers": "2",
            "esrb": "T",
            # Stored but not currently surfaced in MetadataPayload.
            "franchise": "Big Franchise",
        }
        payload = libretro_metadat.entry_to_metadata(entry)
        assert payload["genre"] == "Shooter"
        assert payload["developer"] == "Studio X"
        assert payload["publisher"] == "Big Pub"
        assert payload["release_year"] == 2003
        assert payload["players"] == "2"
        assert payload["rating"] == "T"
        # franchise isn't mapped to a payload key today.
        assert "franchise" not in payload


class TestLibretroMetadatLoad:
    def test_load_via_resolve_for_gba(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stage fixture files + patch install dir so the resolver finds them."""
        libretro_metadat.reset_cache_for_tests()
        _stage_libretro_metadat(tmp_path, "Nintendo - Game Boy Advance")
        monkeypatch.setattr(
            "romulus.app._resolve_install_dir", lambda: tmp_path
        )
        idx = libretro_metadat.get_index_for_system("gba")
        assert idx is not None
        assert sorted(idx.by_dim) == [
            "developer", "genre", "maxusers", "publisher", "releaseyear"
        ]
        hit = libretro_metadat.lookup_by_crc32("56C83C16", idx)
        assert hit is not None
        assert hit["genre"] == "Shooter"
        assert hit["developer"] == "Studio X"
        assert hit["releaseyear"] == "2003"

    def test_no_files_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        libretro_metadat.reset_cache_for_tests()
        monkeypatch.setattr(
            "romulus.app._resolve_install_dir", lambda: tmp_path
        )
        assert libretro_metadat.get_index_for_system("gba") is None


class TestLibretroMetadatEnrichmentChain:
    def test_libretro_fires_before_gamedb(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When libretro has a match, GameDB / Hasheous / TGDB never get called."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        libretro_metadat.reset_cache_for_tests()
        gamedb.reset_cache_for_tests()

        # Stage libretro fixture and point the resolver at it.
        _stage_libretro_metadat(tmp_path, "Nintendo - Game Boy Advance")
        monkeypatch.setattr(
            "romulus.app._resolve_install_dir", lambda: tmp_path
        )

        # Seed a GBA ROM with CRC32 matching the fixture entry.
        rom_id = _seed_verified_game(
            seeded_db, title="Game One", system_id="gba", sha1="a" * 40
        )
        q.upsert_hash(
            seeded_db, rom_id, crc32="56c83c16", sha1="a" * 40, md5=None
        )
        seeded_db.commit()

        # Track non-libretro provider hits.
        gamedb_hits = []
        monkeypatch.setattr(
            gamedb,
            "get_index_for_system",
            lambda sid: gamedb_hits.append(sid) or None,
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )

        assert stats["metadata_added"] == 1
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "libretro_metadat"
        assert meta["genre"] == "Shooter"
        assert meta["developer"] == "Studio X"
        assert meta["publisher"] == "Big Pub"
        assert meta["release_year"] == 2003
        assert meta["players"] == "2"
        # GameDB resolver was never asked because libretro committed first.
        assert gamedb_hits == []

    def test_libretro_miss_falls_through_to_gamedb(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty libretro payload (e.g. franchise-only hit) must not lock the chain."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        libretro_metadat.reset_cache_for_tests()
        gamedb.reset_cache_for_tests()

        # Stage ONLY a franchise file (not mapped to any MetadataPayload key)
        # so libretro returns a non-empty raw entry but an empty payload.
        franchise_dir = tmp_path / "data" / "libretro-metadat" / "franchise"
        franchise_dir.mkdir(parents=True)
        (franchise_dir / "Nintendo - Game Boy Advance.dat").write_text(
            'game (\n\trom ( crc 56C83C16 )\n\tfranchise "Big Franchise"\n)\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "romulus.app._resolve_install_dir", lambda: tmp_path
        )

        rom_id = _seed_verified_game(
            seeded_db, title="Game", system_id="gba", sha1="b" * 40
        )
        q.upsert_hash(
            seeded_db, rom_id, crc32="56c83c16", sha1="b" * 40, md5=None
        )
        seeded_db.commit()

        # GameDB resolver returns a stub index with publisher data.
        stub_index = gamedb.GameDBIndex([
            {
                "crc32": "0x56c83c16",
                "publisher": "GameDB Pub",
                "release_date": "2003-01-01",
                "title": "Game",
            }
        ])
        monkeypatch.setattr(
            gamedb,
            "get_index_for_system",
            lambda sid: stub_index if sid == "gba" else None,
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )

        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        # Libretro had only franchise (no user-facing payload), so the
        # chain progressed to GameDB which had publisher data.
        assert meta["source"] == "gamedb"
        assert meta["publisher"] == "GameDB Pub"


# ---------------------------------------------------------------------------
# include_online flag — gates Hasheous / ScreenScraper / TheGamesDB
# ---------------------------------------------------------------------------


class TestEnrichIncludeOnlineFlag:
    """``include_online=False`` must skip every network-touching provider."""

    def test_offline_only_skips_remote_when_local_misses(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No libretro / GameDB hit, include_online=False -> no HTTP calls."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        set_config(seeded_db, "thegamesdb_api_key", "KEY")
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(thegamesdb, "MIN_REQUEST_INTERVAL", 0.0)
        libretro_metadat.reset_cache_for_tests()
        gamedb.reset_cache_for_tests()
        # Force both local sources to miss.
        monkeypatch.setattr(
            libretro_metadat, "get_index_for_system", lambda _sid: None
        )
        monkeypatch.setattr(
            gamedb, "get_index_for_system", lambda _sid: None
        )

        _seed_verified_game(
            seeded_db, title="Game", system_id="gb", sha1="0" * 40
        )

        non_cover_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "thumbnails.libretro.com" not in request.url.host:
                non_cover_hosts.append(request.url.host)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
            include_online=False,
        )

        assert stats["games_processed"] == 1
        assert stats["metadata_added"] == 0
        # No metadata-source host was contacted; only the cover CDN.
        assert non_cover_hosts == [], (
            f"Expected no metadata API calls; got: {non_cover_hosts}"
        )

    def test_offline_only_still_lets_local_hits_through(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local libretro hits aren't gated by include_online."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        libretro_metadat.reset_cache_for_tests()
        gamedb.reset_cache_for_tests()
        _stage_libretro_metadat(tmp_path, "Nintendo - Game Boy Advance")
        monkeypatch.setattr(
            "romulus.app._resolve_install_dir", lambda: tmp_path
        )

        rom_id = _seed_verified_game(
            seeded_db, title="Game One", system_id="gba", sha1="1" * 40
        )
        q.upsert_hash(
            seeded_db, rom_id, crc32="56c83c16", sha1="1" * 40, md5=None
        )
        seeded_db.commit()

        def boom(_request: httpx.Request) -> httpx.Response:
            # Cover fetch is allowed; metadata APIs should never fire.
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(boom))
        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
            include_online=False,
        )
        assert stats["metadata_added"] == 1
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "libretro_metadat"

    def test_online_default_runs_remote_when_local_misses(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """include_online defaults to True; Hasheous fires when local misses."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        libretro_metadat.reset_cache_for_tests()
        gamedb.reset_cache_for_tests()
        monkeypatch.setattr(
            libretro_metadat, "get_index_for_system", lambda _sid: None
        )
        monkeypatch.setattr(
            gamedb, "get_index_for_system", lambda _sid: None
        )

        rom_id = _seed_verified_game(
            seeded_db, title="Game", system_id="gb", sha1="2" * 40
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "hasheous" in request.url.host:
                return httpx.Response(
                    200, json={"title": "Game", "description": "hit"}
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        stats = enrich_library(
            seeded_db, cache_dir=tmp_path / "covers", http_client=client
        )
        assert stats["metadata_added"] == 1
        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "hasheous"


# ---------------------------------------------------------------------------
# fetch_online_covers_for_scope — the metadata/covers split's online side
# ---------------------------------------------------------------------------


class TestFetchOnlineCoversForScope:
    def test_inserts_libretro_covers_for_scope(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """The function should request a thumbnail and insert a cover row."""
        from romulus.metadata import fetch_online_covers_for_scope

        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        rom_id = _seed_verified_game(
            seeded_db,
            title="Super Mario World",
            system_id="snes",
            sha1="a" * 40,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "thumbnails.libretro.com" in host:
                if "Named_Boxarts" in request.url.path:
                    return httpx.Response(
                        200, content=b"\x89PNG\r\n\x1a\nBOX"
                    )
                return httpx.Response(404)
            raise AssertionError(f"unexpected host: {host}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        # get_rom_ids_for_scope resolves a single rom_id scope.
        rom_ids = q.get_rom_ids_for_scope(seeded_db, rom_id=rom_id)
        n = fetch_online_covers_for_scope(
            seeded_db,
            scope_rom_ids=rom_ids,
            cache_dir=tmp_path / "covers",
            http_client=client,
        )
        assert n == 1
        covers = q.get_covers(seeded_db, rom_id)
        assert len(covers) == 1

    def test_empty_scope_returns_zero(
        self, seeded_db, tmp_path: Path
    ) -> None:
        """An explicit empty list should short-circuit without any work."""
        from romulus.metadata import fetch_online_covers_for_scope

        n = fetch_online_covers_for_scope(
            seeded_db,
            scope_rom_ids=[],
            cache_dir=tmp_path / "covers",
        )
        assert n == 0


# ---------------------------------------------------------------------------
# TestSiblingMetadataCopy — sibling-copy gate in the enrichment chain
# ---------------------------------------------------------------------------


class TestSiblingMetadataCopy:
    """Verify that the sibling-copy gate copies metadata from an existing ROM
    and skips every source provider so no network quota is spent.

    Three scenarios: SHA-1 match, canonical_name match, and no-sibling fallthrough.
    """

    def _make_mock_client(self, call_log: list[str]) -> httpx.Client:
        """Return a client that records every host contacted."""
        def handler(request: httpx.Request) -> httpx.Response:
            call_log.append(request.url.host)
            return httpx.Response(404)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_sibling_copy_via_sha1(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two ROMs with identical SHA-1: second copies metadata from first, zero API calls."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(
            libretro_metadat, "get_index_for_system", lambda _sid: None
        )

        shared_sha1 = "ab" * 20  # 40-char hex

        # First ROM — enrich it directly so it already has a metadata row.
        rom1_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Mario.sfc",
                "filename": "Mario.sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
                "title": "Super Mario World",
                "canonical_name": "Super Mario World",
                "dat_match": "Super Mario World",
                "match_confidence": "dat_verified",
            },
        )
        q.upsert_hash(seeded_db, rom1_id, crc32="aabbccdd", sha1=shared_sha1, md5=None)
        q.upsert_metadata(
            seeded_db,
            rom1_id,
            {"description": "Mario in Dinosaur Land.", "genre": "Platformer"},
            source="hasheous",
        )

        # Second ROM — same SHA-1, no metadata yet.
        rom2_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/snes/Mario (Alt).sfc",
                "filename": "Mario (Alt).sfc",
                "extension": ".sfc",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "snes",
                "title": "Super Mario World",
                "canonical_name": "Super Mario World",
                "dat_match": "Super Mario World",
                "match_confidence": "dat_verified",
            },
        )
        q.upsert_hash(seeded_db, rom2_id, crc32="aabbccdd", sha1=shared_sha1, md5=None)
        seeded_db.commit()

        api_calls: list[str] = []
        client = self._make_mock_client(api_calls)

        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
        )

        # The second ROM should have been enriched via sibling-copy.
        assert stats["games_processed"] == 1
        assert stats["metadata_added"] == 1

        meta2 = q.get_metadata(seeded_db, rom2_id)
        assert meta2 is not None
        assert meta2["description"] == "Mario in Dinosaur Land."
        assert meta2["genre"] == "Platformer"

        # No metadata API should have been contacted (the sibling gate fired first).
        metadata_api_hosts = [
            h for h in api_calls
            if "hasheous" in h or "thegamesdb" in h or "screenscraper" in h
        ]
        assert metadata_api_hosts == [], (
            f"Expected zero metadata API calls; got: {metadata_api_hosts}"
        )

    def test_sibling_copy_via_canonical_name(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two ROMs with same (system_id, canonical_name) but no SHA-1: sibling-copy fires."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(
            libretro_metadat, "get_index_for_system", lambda _sid: None
        )

        # First ROM — has metadata, no hash row.
        rom1_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Tetris.gb",
                "filename": "Tetris.gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Tetris",
                "canonical_name": "Tetris (World)",
                "dat_match": "Tetris (World)",
                "match_confidence": "dat_verified",
            },
        )
        q.upsert_metadata(
            seeded_db,
            rom1_id,
            {"description": "Classic falling blocks.", "genre": "Puzzle"},
            source="gamedb",
        )

        # Second ROM — same system_id + canonical_name, no SHA-1, no metadata.
        rom2_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Tetris (Rev B).gb",
                "filename": "Tetris (Rev B).gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "Tetris",
                "canonical_name": "Tetris (World)",
                "dat_match": "Tetris (World)",
                "match_confidence": "dat_verified",
            },
        )
        seeded_db.commit()

        api_calls: list[str] = []
        client = self._make_mock_client(api_calls)

        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
        )

        assert stats["games_processed"] == 1
        assert stats["metadata_added"] == 1

        meta2 = q.get_metadata(seeded_db, rom2_id)
        assert meta2 is not None
        assert meta2["description"] == "Classic falling blocks."
        assert meta2["genre"] == "Puzzle"

        metadata_api_hosts = [
            h for h in api_calls
            if "hasheous" in h or "thegamesdb" in h or "screenscraper" in h
        ]
        assert metadata_api_hosts == [], (
            "Expected zero metadata API calls via canonical_name sibling; "
            f"got: {metadata_api_hosts}"
        )

    def test_no_sibling_falls_through_to_source_chain(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no sibling exists, the full source chain runs normally."""
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(
            libretro_metadat, "get_index_for_system", lambda _sid: None
        )

        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Zelda.gb",
                "filename": "Zelda.gb",
                "extension": ".gb",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "gb",
                "title": "The Legend of Zelda: Link's Awakening",
                "canonical_name": "Legend of Zelda, The - Link's Awakening (USA, Europe)",
                "dat_match": "Legend of Zelda, The - Link's Awakening (USA, Europe)",
                "match_confidence": "dat_verified",
            },
        )
        q.upsert_hash(seeded_db, rom_id, crc32="12345678", sha1="cc" * 20, md5=None)
        seeded_db.commit()

        hasheous_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "hasheous" in host:
                hasheous_calls.append(host)
                return httpx.Response(
                    200,
                    json={"title": "Zelda", "description": "Adventure in Koholint."},
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        stats = enrich_library(
            seeded_db,
            cache_dir=tmp_path / "covers",
            http_client=client,
        )

        assert stats["games_processed"] == 1
        assert stats["metadata_added"] == 1
        # Hasheous was reached because there was no sibling to copy from.
        assert len(hasheous_calls) == 1

        meta = q.get_metadata(seeded_db, rom_id)
        assert meta is not None
        assert meta["source"] == "hasheous"
        assert meta["description"] == "Adventure in Koholint."
