"""Tests for metadata clients — libretro thumbnails, Hasheous, LaunchBox.

All HTTP traffic is intercepted via `httpx.MockTransport`. No real network
requests are issued from these tests.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from romulus.db import queries as q
from romulus.db import seed_defaults, set_config
from romulus.metadata import enrich_library, hasheous, launchbox, libretro, screenscraper

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
        game_id = q.upsert_game(seeded_db, {"title": "Tetris", "system_id": "gb"})
        q.upsert_metadata(
            seeded_db,
            game_id,
            {"description": "Falling blocks.", "genre": "Puzzle"},
            source="hasheous",
        )
        row = q.get_metadata(seeded_db, game_id)
        assert row is not None
        assert row["description"] == "Falling blocks."
        assert row["genre"] == "Puzzle"
        assert row["source"] == "hasheous"

    def test_upsert_replaces_existing(self, seeded_db) -> None:
        game_id = q.upsert_game(seeded_db, {"title": "Tetris", "system_id": "gb"})
        q.upsert_metadata(
            seeded_db, game_id, {"description": "v1"}, source="hasheous"
        )
        q.upsert_metadata(
            seeded_db, game_id, {"description": "v2"}, source="launchbox"
        )
        row = q.get_metadata(seeded_db, game_id)
        assert row["description"] == "v2"
        assert row["source"] == "launchbox"

    def test_insert_and_get_covers(self, seeded_db) -> None:
        game_id = q.upsert_game(seeded_db, {"title": "Tetris", "system_id": "gb"})
        q.insert_cover(
            seeded_db,
            game_id,
            "Named_Boxarts",
            "https://example.com/Tetris.png",
            "/tmp/Tetris.png",
        )
        covers = q.get_covers(seeded_db, game_id)
        assert len(covers) == 1
        assert covers[0]["cover_type"] == "Named_Boxarts"
        assert q.has_cover(seeded_db, game_id, "Named_Boxarts") is True
        assert q.has_cover(seeded_db, game_id, "Named_Snaps") is False

    def test_get_games_needing_enrichment(self, seeded_db) -> None:
        # Verified game with no metadata -> appears.
        game_id = q.upsert_game(
            seeded_db, {"title": "Tetris", "system_id": "gb", "canonical_name": "Tetris (World)"}
        )
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Tetris.gb",
                "filename": "Tetris.gb",
                "extension": ".gb",
                "size_bytes": 32768,
                "mtime": time.time(),
                "system_id": "gb",
                "dat_match": "Tetris (World)",
                "match_confidence": "dat_verified",
            },
        )
        q.link_rom_to_game(seeded_db, rom_id, game_id)
        seeded_db.commit()

        rows = q.get_games_needing_enrichment(seeded_db)
        assert len(rows) == 1
        assert rows[0]["title"] == "Tetris"

        # Now add metadata — it should disappear from the queue.
        q.upsert_metadata(seeded_db, game_id, {"description": "x"}, source="hasheous")
        rows = q.get_games_needing_enrichment(seeded_db)
        assert rows == []

    def test_unverified_games_are_excluded(self, seeded_db) -> None:
        game_id = q.upsert_game(seeded_db, {"title": "Mystery", "system_id": "gb"})
        rom_id = q.upsert_rom(
            seeded_db,
            {
                "path": "/lib/gb/Mystery.gb",
                "filename": "Mystery.gb",
                "extension": ".gb",
                "size_bytes": 1024,
                "mtime": time.time(),
                "system_id": "gb",
                "match_confidence": "fuzzy",
            },
        )
        q.link_rom_to_game(seeded_db, rom_id, game_id)
        seeded_db.commit()
        assert q.get_games_needing_enrichment(seeded_db) == []


# ---------------------------------------------------------------------------
# enrich_library orchestrator
# ---------------------------------------------------------------------------


def _seed_verified_game(db, *, title: str, system_id: str, sha1: str | None) -> int:
    """Insert a verified game + linked ROM, returning the game id."""
    game_id = q.upsert_game(
        db,
        {"title": title, "system_id": system_id, "canonical_name": title},
    )
    rom_id = q.upsert_rom(
        db,
        {
            "path": f"/lib/{system_id}/{title}.bin",
            "filename": f"{title}.bin",
            "extension": ".bin",
            "size_bytes": 1024,
            "mtime": time.time(),
            "system_id": system_id,
            "dat_match": title,
            "match_confidence": "dat_verified",
        },
    )
    q.link_rom_to_game(db, rom_id, game_id)
    if sha1:
        q.upsert_hash(db, rom_id, crc32="ffffffff", sha1=sha1, md5=None)
    db.commit()
    return game_id


class TestEnrichLibrary:
    def test_enrich_writes_metadata_and_cover(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        sha1 = "a" * 40
        _seed_verified_game(
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
                if "Named_Boxarts" in request.url.path:
                    return httpx.Response(200, content=b"\x89PNGBOX")
                return httpx.Response(404)
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
        assert stats["covers_added"] == 1
        assert progress_events == [(1, 1, "Super Mario World")]

        meta = q.get_metadata(seeded_db, 1)
        assert meta is not None
        assert meta["description"] == "Mario in Dinosaur Land."
        assert meta["source"] == "hasheous"

        covers = q.get_covers(seeded_db, 1)
        assert len(covers) == 1
        assert Path(covers[0]["local_path"]).exists()

    def test_enrich_falls_back_to_launchbox(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        _seed_verified_game(
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
        meta = q.get_metadata(seeded_db, 1)
        assert meta is not None
        assert meta["source"] == "launchbox"
        assert meta["developer"] == "Nintendo EAD"

    def test_enrich_skips_already_enriched(
        self, seeded_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_defaults(seeded_db)
        set_config(seeded_db, "cover_cache_path", str(tmp_path / "covers"))
        monkeypatch.setattr(hasheous, "MIN_REQUEST_INTERVAL", 0.0)

        game_id = _seed_verified_game(
            seeded_db, title="Tetris", system_id="gb", sha1="c" * 40
        )
        q.upsert_metadata(
            seeded_db, game_id, {"description": "already here"}, source="manual"
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
