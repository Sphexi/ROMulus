"""Tests for filename parsing — tag extraction, region/revision/status flags."""

from __future__ import annotations

import pytest

from romulus.core.scanner import parse_filename


class TestExtensionExtraction:
    def test_lowercase_extension(self):
        assert parse_filename("Game.SFC").extension == ".sfc"

    def test_dot_included(self):
        assert parse_filename("Game.gba").extension == ".gba"

    def test_compound_extension_gets_last_segment(self):
        # parse_filename takes the final suffix only.
        result = parse_filename("Game.nkit.iso")
        assert result.extension == ".iso"


class TestCleanName:
    def test_simple_name(self):
        assert parse_filename("Sonic the Hedgehog.md").clean_name == "Sonic the Hedgehog"

    def test_strips_region_tag(self):
        assert parse_filename("Sonic the Hedgehog (USA).md").clean_name == "Sonic the Hedgehog"

    def test_strips_multiple_tags(self):
        result = parse_filename("Sonic the Hedgehog (USA) (Rev 1) [!].md")
        assert result.clean_name == "Sonic the Hedgehog"

    def test_collapses_whitespace(self):
        assert parse_filename("Sonic   the   Hedgehog (USA).md").clean_name == "Sonic the Hedgehog"

    def test_strips_trailing_separators(self):
        # Some No-Intro names end with a separator after removing tags.
        result = parse_filename("Game - (USA).smc")
        # Trailing " -" gets cleaned up.
        assert result.clean_name == "Game"


class TestRegionParsing:
    def test_usa(self):
        assert parse_filename("Game (USA).smc").region == "USA"

    def test_europe(self):
        assert parse_filename("Game (Europe).smc").region == "Europe"

    def test_japan(self):
        assert parse_filename("Game (Japan).smc").region == "Japan"

    def test_world(self):
        assert parse_filename("Game (World).smc").region == "World"

    def test_multi_region(self):
        assert parse_filename("Game (USA, Europe).smc").region == "USA, Europe"

    def test_multi_region_with_language(self):
        # No-Intro often uses (USA, Europe) (En,Fr,De) — the first matched group wins.
        result = parse_filename("Game (USA, Europe).smc")
        assert result.region == "USA, Europe"

    def test_no_region(self):
        assert parse_filename("Game.smc").region is None

    def test_unknown_region_does_not_match(self):
        # "Foo" is not a region token — should leave region empty.
        assert parse_filename("Game (Foo).smc").region is None


class TestRevisionParsing:
    def test_rev_numeric(self):
        assert parse_filename("Game (USA) (Rev 1).smc").revision == "Rev 1"

    def test_rev_letter(self):
        assert parse_filename("Game (USA) (Rev A).smc").revision == "Rev A"

    def test_v_version(self):
        assert parse_filename("Game (USA) (v1.1).smc").revision == "v1.1"

    def test_no_revision(self):
        assert parse_filename("Game (USA).smc").revision is None


class TestStatusFlags:
    def test_verified(self):
        result = parse_filename("Game (USA) [!].smc")
        assert result.is_verified is True
        assert "verified" in result.status

    def test_bad_dump(self):
        result = parse_filename("Game (USA) [b].smc")
        assert result.is_bad_dump is True
        assert "bad_dump" in result.status

    def test_bad_dump_numbered(self):
        # [b1] should also count.
        assert parse_filename("Game [b1].smc").is_bad_dump is True

    def test_hack(self):
        result = parse_filename("Game [h].smc")
        assert result.is_hack is True
        assert "hack" in result.status

    def test_hack_numbered(self):
        assert parse_filename("Game [h2].smc").is_hack is True

    def test_translation(self):
        result = parse_filename("Game [T+Eng].smc")
        assert result.is_translation is True
        assert "translation" in result.status

    def test_translation_old_style(self):
        assert parse_filename("Game [T-Eng].smc").is_translation is True

    def test_unlicensed(self):
        result = parse_filename("Game (Unl).nes")
        assert result.is_unlicensed is True

    def test_prototype(self):
        result = parse_filename("Game (Proto).smc")
        assert result.is_prototype is True

    def test_prototype_long(self):
        assert parse_filename("Game (Prototype).smc").is_prototype is True

    def test_beta(self):
        assert parse_filename("Game (Beta).smc").is_beta is True

    def test_demo(self):
        assert parse_filename("Game (Demo).smc").is_demo is True

    def test_sample(self):
        result = parse_filename("Game (Sample).smc")
        assert "sample" in result.status

    def test_combined_flags(self):
        # No-Intro frequently combines tags: (USA) (Rev 1) [!]
        result = parse_filename("Game (USA) (Rev 1) [!].smc")
        assert result.region == "USA"
        assert result.revision == "Rev 1"
        assert result.is_verified is True


class TestDiscParsing:
    def test_disc_numeric(self):
        assert parse_filename("Game (Disc 1).iso").disc_number == 1

    def test_disc_two(self):
        assert parse_filename("Game (USA) (Disc 2).iso").disc_number == 2

    def test_disc_letter(self):
        # "Disc A" → 1, "Disc B" → 2
        assert parse_filename("Game (Disc A).iso").disc_number == 1
        assert parse_filename("Game (Disc B).iso").disc_number == 2

    def test_disk_alias(self):
        assert parse_filename("Game (Disk 1).adf").disc_number == 1

    def test_disc_of(self):
        # "Disc 1 of 3" — extract 1.
        assert parse_filename("Game (Disc 1 of 3).iso").disc_number == 1

    def test_no_disc(self):
        assert parse_filename("Game.iso").disc_number is None


class TestUnknownTagsDropped:
    def test_unknown_tag_stripped_from_clean(self):
        # Random vendor tag should NOT appear in clean_name.
        result = parse_filename("Game (RetroPie).smc")
        assert "RetroPie" not in result.clean_name

    def test_multiple_unknown_tags_dropped(self):
        result = parse_filename("Game (Foo) (Bar).smc")
        assert result.clean_name == "Game"


class TestDisplayTitle:
    def test_trailing_article_moves_to_front(self):
        assert parse_filename("Addams Family, The.smc").display_title == "The Addams Family"

    def test_no_trailing_article_unchanged(self):
        assert parse_filename("The Addams Family.smc").display_title == "The Addams Family"

    def test_works_with_other_languages(self):
        # "Histoire, La" → "La Histoire" (French).
        result = parse_filename("Histoire, La.gba")
        assert result.display_title == "La Histoire"


class TestReleaseType:
    """Re-release / port tags (Virtual Console etc.) are identity-bearing —
    they must NOT be silently stripped or the cartridge dump collapses into
    the same fuzzy_key as the VC release.
    """

    def test_virtual_console_tag_extracted(self):
        result = parse_filename("Alien Soldier (USA) (Virtual Console).zip")
        assert result.release_type == "Virtual Console"
        # Region tag still works in parallel.
        assert result.region == "USA"

    def test_switch_online_tag_extracted(self):
        result = parse_filename("F-Zero (USA) (Switch Online).sfc")
        assert result.release_type == "Switch Online"

    def test_genesis_mini_tag_extracted(self):
        result = parse_filename("Sonic (USA) (Genesis Mini).md")
        assert result.release_type == "Genesis Mini"

    def test_no_release_tag(self):
        result = parse_filename("Sonic (USA).md")
        assert result.release_type is None

    def test_unknown_paren_does_not_become_release_type(self):
        result = parse_filename("Game (RetroPie).smc")
        assert result.release_type is None

    def test_display_title_includes_release_type(self):
        """User sees a distinct title for the VC release in the game list."""
        result = parse_filename("Alien Soldier (USA) (Virtual Console).zip")
        assert "Virtual Console" in result.display_title
        assert result.display_title == "Alien Soldier (Virtual Console)"

    def test_display_title_omits_when_no_release_type(self):
        result = parse_filename("Alien Soldier (USA).zip")
        assert "(" not in result.display_title  # no spurious tag added


@pytest.mark.parametrize(
    "filename,expected_clean",
    [
        ("Sonic the Hedgehog (USA, Europe).md", "Sonic the Hedgehog"),
        ("Sonic the Hedgehog (Japan).md", "Sonic the Hedgehog"),
        ("Final Fantasy VI (USA).smc", "Final Fantasy VI"),
        ("Castlevania - Symphony of the Night (USA).bin", "Castlevania - Symphony of the Night"),
        ("Pokemon Red (USA, Europe) (SGB Enhanced).gb", "Pokemon Red"),
        ("Mortal Kombat 3 (USA) (Rev A).md", "Mortal Kombat 3"),
        ("Doom (USA) (Rev 1).32x", "Doom"),
    ],
)
def test_real_world_filenames(filename, expected_clean):
    assert parse_filename(filename).clean_name == expected_clean
