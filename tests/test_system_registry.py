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
