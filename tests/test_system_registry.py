"""Tests for system registry — seeding, extension lookups, alias matching."""

from __future__ import annotations

import json

from romulus.models import SYSTEM_REGISTRY, SystemDef
from romulus.models.system import (
    get_extensions_by_system,
    get_systems_by_alias,
    seed_systems,
)


def test_registry_has_at_least_thirty_systems():
    assert len(SYSTEM_REGISTRY) >= 30


def test_registry_ids_are_unique():
    ids = [s.id for s in SYSTEM_REGISTRY]
    assert len(ids) == len(set(ids))


def test_registry_includes_expected_core_systems():
    ids = {s.id for s in SYSTEM_REGISTRY}
    required = {"nes", "snes", "n64", "gb", "gbc", "gba", "nds", "megadrive", "psx"}
    assert required.issubset(ids)


def test_registry_ids_are_lowercase_alphanumeric():
    for s in SYSTEM_REGISTRY:
        assert s.id == s.id.lower()
        assert s.id.replace("_", "").isalnum() or s.id.isalnum()


def test_registry_extensions_have_leading_dot():
    for s in SYSTEM_REGISTRY:
        for ext in s.extensions:
            assert ext.startswith("."), f"{s.id} extension {ext!r} missing leading dot"
            assert ext == ext.lower(), f"{s.id} extension {ext!r} not lowercase"


def test_registry_folder_aliases_are_lowercase():
    for s in SYSTEM_REGISTRY:
        for alias in s.folder_aliases:
            assert alias == alias.lower(), f"{s.id} alias {alias!r} not lowercase"


def test_header_rules_are_valid():
    valid = {None, "smc_512", "ines_16", "n64_byteswap", "lynx_64"}
    for s in SYSTEM_REGISTRY:
        assert s.header_rule in valid, f"{s.id} has invalid header_rule {s.header_rule!r}"


def test_genesis_alias_maps_to_megadrive():
    md = next(s for s in SYSTEM_REGISTRY if s.id == "megadrive")
    assert "genesis" in md.folder_aliases


def test_snes_has_smc_header_rule():
    snes = next(s for s in SYSTEM_REGISTRY if s.id == "snes")
    assert snes.header_rule == "smc_512"


def test_nes_has_ines_header_rule():
    nes = next(s for s in SYSTEM_REGISTRY if s.id == "nes")
    assert nes.header_rule == "ines_16"


def test_n64_has_byteswap_header_rule():
    n64 = next(s for s in SYSTEM_REGISTRY if s.id == "n64")
    assert n64.header_rule == "n64_byteswap"


def test_lynx_has_lynx_header_rule():
    lynx = next(s for s in SYSTEM_REGISTRY if s.id == "lynx")
    assert lynx.header_rule == "lynx_64"


class TestSeedSystems:
    def test_seed_inserts_all_systems(self, db):
        inserted = seed_systems(db)
        assert inserted == len(SYSTEM_REGISTRY)
        count = db.execute("SELECT COUNT(*) FROM systems").fetchone()[0]
        assert count == len(SYSTEM_REGISTRY)

    def test_seed_is_idempotent(self, db):
        seed_systems(db)
        second = seed_systems(db)
        assert second == 0

    def test_seeded_rows_round_trip_json_fields(self, db):
        seed_systems(db)
        row = db.execute(
            "SELECT extensions, folder_aliases FROM systems WHERE id = 'snes'"
        ).fetchone()
        extensions = json.loads(row[0])
        aliases = json.loads(row[1])
        assert ".sfc" in extensions
        assert "snes" in aliases


class TestAliasLookup:
    def test_get_systems_by_alias_flattens_all_aliases(self, db):
        seed_systems(db)
        alias_map = get_systems_by_alias(db)
        assert alias_map["snes"] == "snes"
        assert alias_map["sfc"] == "snes"
        assert alias_map["superfamicom"] == "snes"

    def test_genesis_alias_resolves_to_megadrive_system(self, db):
        seed_systems(db)
        alias_map = get_systems_by_alias(db)
        assert alias_map["genesis"] == "megadrive"
        assert alias_map["md"] == "megadrive"

    def test_alias_lookup_is_lowercase_keyed(self, db):
        seed_systems(db)
        alias_map = get_systems_by_alias(db)
        # All keys should already be lowercase.
        assert all(k == k.lower() for k in alias_map)


class TestExtensionLookup:
    def test_extensions_returned_per_system(self, db):
        seed_systems(db)
        ext_map = get_extensions_by_system(db)
        assert ".sfc" in ext_map["snes"]
        assert ".gba" in ext_map["gba"]
        assert ".md" in ext_map["megadrive"]


def test_systemdef_pydantic_validates():
    s = SystemDef(
        id="test",
        display_name="Test",
        short_name="T",
        extensions=[".tst"],
        folder_aliases=["test", "tst"],
    )
    assert s.id == "test"


# ---------------------------------------------------------------------------
# v0.1.0 DAT-driven expansion — registry now covers the bundled No-Intro corpus
# ---------------------------------------------------------------------------


def test_registry_grew_to_cover_bundled_dats():
    """The v0.1.0 expansion adds ~38 SystemDefs to cover bundled No-Intro DATs.

    The v0.1.0 final pass adds 9 more SystemDefs for the digital-install
    era (Wii, Wii U, 3DS, DSiWare, PS Vita, PS3, Xbox 360, J2ME, Palm OS).
    Locking the count in prevents accidental deletions during refactors. If a
    legitimate addition or removal is made, update this number deliberately.
    """
    assert len(SYSTEM_REGISTRY) == 80


def test_expansion_ids_are_all_present():
    """Sample of the v0.1.0 expansion — covers each manufacturer block added."""
    ids = {s.id for s in SYSTEM_REGISTRY}
    expected_new = {
        # Atari (extended)
        "atari5200", "jaguar",
        # Bandai / Benesse
        "wonderswan", "wonderswancolor", "pocketchallengev2",
        # Casio
        "casioloopy", "pv1000",
        # Coleco / Mattel / GCE / Magnavox / RCA / Emerson / Entex / Epoch
        "colecovision", "intellivision", "vectrex", "odyssey2", "studio2",
        "arcadia2001", "adventurevision", "scv",
        # Fairchild / Funtech / Hartung / Tiger / Watara / Konami
        "channelf", "superacan", "gamemaster", "gamecom", "supervision", "picno",
        # VTech / LeapFrog
        "creativision", "vsmile", "leappad", "leapster", "myfirstleappad",
        # Commodore extras
        "vic20", "c64plus4",
        # NEC extended
        "supergrafx",
        # Nintendo accessories / spinoffs
        "n64dd", "pokemini", "satellaview", "sufami", "ereader",
        # Sega extended
        "sg1000", "segapico", "beena",
        # Korean handheld
        "gp32",
    }
    missing = expected_new - ids
    assert not missing, f"v0.1.0 expansion missing ids: {sorted(missing)}"


def test_dat_name_aliases_unique_across_registry():
    """No two SystemDefs may claim the same dat_name or alias — otherwise the
    DAT->system resolver becomes ambiguous and the wrong system_id may stick
    to imported ROMs.
    """
    seen: dict[str, str] = {}
    for s in SYSTEM_REGISTRY:
        keys: list[str] = []
        if s.dat_name:
            keys.append(s.dat_name)
        keys.extend(s.dat_name_aliases)
        for key in keys:
            assert key not in seen, (
                f"DAT name {key!r} is claimed by both {seen[key]!r} and {s.id!r}"
            )
            seen[key] = s.id


def test_videopac_plus_aliased_to_odyssey2():
    """Philips Videopac+ is the same hardware as Magnavox Odyssey 2 (G7400)."""
    o2 = next(s for s in SYSTEM_REGISTRY if s.id == "odyssey2")
    assert "Philips - Videopac+" in o2.dat_name_aliases


def test_dsi_decrypted_aliased_to_nds():
    """DSi cart dumps share the DS cart slot — treat as the same logical system."""
    nds = next(s for s in SYSTEM_REGISTRY if s.id == "nds")
    assert "Nintendo - Nintendo DSi (Decrypted)" in nds.dat_name_aliases


# ---------------------------------------------------------------------------
# v0.1.0 final pass — digital-install / eShop / PSN / Xbox Live coverage
# ---------------------------------------------------------------------------


def test_digital_install_systems_all_present():
    """The 9 digital-distribution SystemDefs added in the v0.1.0 final pass."""
    ids = {s.id for s in SYSTEM_REGISTRY}
    expected = {
        "wii",
        "wiiu",
        "n3ds",
        "dsiware",
        "psvita",
        "ps3",
        "xbox360",
        "j2me",
        "palmos",
    }
    missing = expected - ids
    assert not missing, f"missing digital-install ids: {sorted(missing)}"


def test_wii_aliases_cover_cdn_and_wad():
    """Both Wii (Digital) (CDN) and Wii (Digital) (WAD) must alias to ``wii``."""
    wii = next(s for s in SYSTEM_REGISTRY if s.id == "wii")
    assert "Nintendo - Wii (Digital) (CDN)" in wii.dat_name_aliases
    assert "Nintendo - Wii (Digital) (WAD)" in wii.dat_name_aliases


def test_n3ds_aliases_cover_all_3ds_variants():
    """The 3DS catalog ships under five distinct No-Intro headers — all
    must resolve to the single ``n3ds`` SystemDef.
    """
    n3ds = next(s for s in SYSTEM_REGISTRY if s.id == "n3ds")
    expected = {
        "Nintendo - Nintendo 3DS (Digital)",
        # Real No-Intro file has literal "(CDN) (CDN)" double-suffix typo.
        "Nintendo - Nintendo 3DS (Digital) (CDN) (CDN)",
        "Nintendo - Nintendo 3DS (Encrypted)",
        "Nintendo - New Nintendo 3DS (Digital)",
        "Nintendo - New Nintendo 3DS (Encrypted)",
    }
    missing = expected - set(n3ds.dat_name_aliases)
    assert not missing, f"3DS aliases missing: {sorted(missing)}"


def test_psp_aliases_cover_psn_and_psx2psp():
    """PSN (Decrypted/Encrypted) and PSX2PSP wrappers all play on PPSSPP —
    same logical system, different delivery channel.
    """
    psp = next(s for s in SYSTEM_REGISTRY if s.id == "psp")
    assert "Sony - PlayStation Portable (PSN) (Decrypted)" in psp.dat_name_aliases
    assert "Sony - PlayStation Portable (PSN) (Encrypted)" in psp.dat_name_aliases
    assert "Sony - PlayStation Portable (PSX2PSP)" in psp.dat_name_aliases


def test_psvita_primary_is_vpk():
    """PS Vita's canonical dat_name is the homebrew VPK header; PSN PKGs are aliases."""
    vita = next(s for s in SYSTEM_REGISTRY if s.id == "psvita")
    assert vita.dat_name == "Sony - PlayStation Vita (VPK)"
    assert "Sony - PlayStation Vita (PSN) (Decrypted)" in vita.dat_name_aliases
    assert "Sony - PlayStation Vita (PSN) (Encrypted)" in vita.dat_name_aliases


# ---------------------------------------------------------------------------
# Profile coverage — sanity check that the v0.1.0 final pass enables
# new systems on at least Batocera (the broadest-coverage profile).
# ---------------------------------------------------------------------------


def test_batocera_enables_new_digital_install_systems():
    """Batocera is the broadest-coverage built-in profile; the 9 new digital-
    install SystemDefs should all have a non-empty folder there. This is a
    sanity check that the profile YAML pass did not leave any of the new
    systems stuck at ``supported: false``.
    """
    from romulus.core.exporter import BUILTIN_PROFILES_DIR, load_profile

    profile = load_profile(BUILTIN_PROFILES_DIR / "batocera.yaml")
    new_ids = {
        "wii",
        "wiiu",
        "n3ds",
        "dsiware",
        "psvita",
        "ps3",
        "xbox360",
        "j2me",
        "palmos",
    }
    for sid in new_ids:
        mapping = profile.systems.get(sid)
        assert mapping is not None, f"batocera missing decision for {sid!r}"
        assert mapping.is_supported, (
            f"batocera should enable {sid!r} but has supported={mapping.supported}, "
            f"folder={mapping.folder!r}"
        )
        assert mapping.folder, (
            f"batocera enabled {sid!r} but folder is empty"
        )
