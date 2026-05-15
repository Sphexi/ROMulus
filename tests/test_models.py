"""Tests for Pydantic data models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from romulus.models import DestinationProfile, Game, RomFile, SystemDef


class TestSystemDef:
    def test_minimal_construction(self):
        s = SystemDef(id="snes", display_name="SNES", short_name="SNES")
        assert s.id == "snes"
        assert s.extensions == []
        assert s.folder_aliases == []

    def test_full_construction(self):
        s = SystemDef(
            id="snes",
            display_name="Super Nintendo Entertainment System",
            short_name="SNES",
            manufacturer="Nintendo",
            generation=4,
            extensions=[".sfc", ".smc"],
            header_rule="smc_512",
            libretro_name="Nintendo - Super Nintendo Entertainment System",
            folder_aliases=["snes", "sfc"],
            dat_name="Nintendo - Super Nintendo Entertainment System",
        )
        assert s.generation == 4
        assert ".sfc" in s.extensions

    def test_required_fields(self):
        with pytest.raises(ValidationError):
            SystemDef()


class TestRomFile:
    def test_required_fields(self):
        rom = RomFile(
            path="/roms/snes/Game.sfc",
            filename="Game.sfc",
            extension=".sfc",
            size_bytes=1024,
            mtime=1700000000.0,
        )
        assert rom.id is None
        assert rom.match_confidence == "unmatched"

    def test_negative_size_rejected(self):
        with pytest.raises(ValidationError):
            RomFile(
                path="/x",
                filename="x",
                extension=".x",
                size_bytes=-1,
                mtime=0.0,
            )


class TestGame:
    def test_basic_construction(self):
        g = Game(title="The Legend of Zelda", system_id="nes")
        assert g.is_hack is False
        assert g.is_homebrew is False
        assert g.is_bios is False

    def test_hack_flag(self):
        g = Game(title="Super Metroid: Redesign", system_id="snes", is_hack=True)
        assert g.is_hack is True


class TestDestinationProfile:
    def test_basic_construction(self):
        # ``base_path`` is RELATIVE to the export target — absolute paths are
        # rejected by the security validator (see security audit v0.1.0
        # finding #1).
        p = DestinationProfile(
            id="anbernic_rg556",
            name="Anbernic RG556",
            base_path="Roms",
            gamelist_format="gamelist_xml",
            systems={
                "snes": {"folder": "snes", "extensions": [".sfc", ".smc"]},
                "nes": {"folder": "nes", "extensions": [".nes"]},
            },
        )
        assert p.systems["snes"].folder == "snes"
        assert p.systems["snes"].is_supported is True
        assert ".sfc" in p.systems["snes"].extensions

    def test_unsupported_system_mapping(self):
        p = DestinationProfile(
            id="example",
            name="Example",
            base_path="roms",
            systems={"gamecube": {"folder": "", "supported": False}},
        )
        assert p.systems["gamecube"].is_supported is False

    def test_absolute_base_path_rejected(self):
        """Absolute base_path values would escape the export target."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="base_path"):
            DestinationProfile(
                id="evil",
                name="Evil",
                base_path="/etc",
                systems={},
            )

    def test_traversal_folder_rejected(self):
        """``..`` segments in system folder would escape the system dir."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="folder"):
            DestinationProfile(
                id="evil",
                name="Evil",
                base_path="roms",
                systems={"snes": {"folder": "../../etc"}},
            )

    def test_windows_drive_letter_rejected(self):
        """Drive-letter prefixes in base_path are an absolute-path bypass."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="base_path"):
            DestinationProfile(
                id="evil",
                name="Evil",
                base_path="C:Roms",
                systems={},
            )

    def test_windows_reserved_name_rejected(self):
        """Windows device names (``CON``, ``PRN``, ...) make paths unusable."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="reserved name"):
            DestinationProfile(
                id="evil",
                name="Evil",
                base_path="CON",
                systems={},
            )
