"""System (platform) data model and registry.

The system registry is the source of truth for which platforms Romulus supports.
Each entry codifies the accepted file extensions, folder-name aliases (across
RetroArch / Batocera / Anbernic / Onion / muOS / ArkOS / ROCKNIX), a header rule
used for normalization prior to hashing, and the libretro thumbnail folder name.

The registry is seeded into the `systems` SQLite table on first run. Adding a
new system is a code change here, not a data-file edit.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, Field, field_validator

HeaderRule = Literal["smc_512", "ines_16", "n64_byteswap", "lynx_64"]


class SystemDef(BaseModel):
    """A supported retro-gaming platform.

    Mirrors the `systems` SQLite table. The `extensions` and `folder_aliases`
    fields are Python lists in memory; they are serialized to JSON strings when
    written to SQLite (the table stores them as TEXT).
    """

    id: str = Field(..., description="Canonical lowercase id (e.g. 'snes', 'gba')")
    display_name: str
    short_name: str
    manufacturer: str | None = None
    generation: int | None = None
    extensions: list[str] = Field(default_factory=list)
    header_rule: HeaderRule | None = Field(
        default=None,
        description="One of: smc_512 | ines_16 | n64_byteswap | lynx_64 | None",
    )
    libretro_name: str | None = None
    folder_aliases: list[str] = Field(default_factory=list)
    dat_name: str | None = None
    dat_name_aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Additional DAT header `<name>` strings that should resolve to this system. "
            "Lets one SystemDef cover No-Intro variants like '(Combined)', '(BigEndian)', "
            "'(Decrypted)', etc. without duplicating registry entries."
        ),
    )

    @field_validator("extensions", mode="after")
    @classmethod
    def _ensure_dot_prefix(cls, v: list[str]) -> list[str]:
        """Reject extensions missing a leading dot; lowercase the rest."""
        for ext in v:
            if not ext.startswith("."):
                raise ValueError(f"extension {ext!r} must start with '.'")
        return [ext.lower() for ext in v]

    @field_validator("folder_aliases", mode="after")
    @classmethod
    def _lowercase_aliases(cls, v: list[str]) -> list[str]:
        """Canonicalize folder aliases to lowercase."""
        return [alias.lower() for alias in v]


# ---------------------------------------------------------------------------
# System registry — ~30 most common platforms.
#
# Extension lists are lowercase and include the leading dot. Folder aliases are
# lowercase. Both are matched case-insensitively at runtime.
# ---------------------------------------------------------------------------

SYSTEM_REGISTRY: list[SystemDef] = [
    # --- Nintendo ---
    SystemDef(
        id="nes",
        display_name="Nintendo Entertainment System",
        short_name="NES",
        manufacturer="Nintendo",
        generation=3,
        extensions=[".nes", ".unf", ".unif", ".fds"],
        header_rule="ines_16",
        libretro_name="Nintendo - Nintendo Entertainment System",
        folder_aliases=["nes", "famicom", "fc"],
        dat_name="Nintendo - Nintendo Entertainment System",
        dat_name_aliases=["Nintendo - Family Computer Disk System"],
    ),
    SystemDef(
        id="snes",
        display_name="Super Nintendo Entertainment System",
        short_name="SNES",
        manufacturer="Nintendo",
        generation=4,
        extensions=[".sfc", ".smc", ".fig", ".swc"],
        header_rule="smc_512",
        libretro_name="Nintendo - Super Nintendo Entertainment System",
        folder_aliases=["snes", "sfc", "superfamicom", "supernintendo", "supernes"],
        dat_name="Nintendo - Super Nintendo Entertainment System",
        dat_name_aliases=["Nintendo - Super Nintendo Entertainment System (Combined)"],
    ),
    SystemDef(
        id="n64",
        display_name="Nintendo 64",
        short_name="N64",
        manufacturer="Nintendo",
        generation=5,
        extensions=[".z64", ".n64", ".v64", ".rom"],
        header_rule="n64_byteswap",
        libretro_name="Nintendo - Nintendo 64",
        folder_aliases=["n64", "nintendo64"],
        dat_name="Nintendo - Nintendo 64",
        dat_name_aliases=["Nintendo - Nintendo 64 (BigEndian)"],
    ),
    SystemDef(
        id="gamecube",
        display_name="Nintendo GameCube",
        short_name="GameCube",
        manufacturer="Nintendo",
        generation=6,
        extensions=[".iso", ".gcm", ".gcz", ".rvz", ".ciso", ".wia", ".nkit.iso"],
        header_rule=None,
        libretro_name="Nintendo - GameCube",
        folder_aliases=["gc", "gamecube", "ngc"],
        dat_name="Nintendo - GameCube",
    ),
    SystemDef(
        id="gb",
        display_name="Game Boy",
        short_name="GB",
        manufacturer="Nintendo",
        generation=4,
        extensions=[".gb"],
        header_rule=None,
        libretro_name="Nintendo - Game Boy",
        folder_aliases=["gb", "gameboy"],
        dat_name="Nintendo - Game Boy",
    ),
    SystemDef(
        id="gbc",
        display_name="Game Boy Color",
        short_name="GBC",
        manufacturer="Nintendo",
        generation=5,
        extensions=[".gbc", ".gb"],
        header_rule=None,
        libretro_name="Nintendo - Game Boy Color",
        folder_aliases=["gbc", "gameboycolor"],
        dat_name="Nintendo - Game Boy Color",
    ),
    SystemDef(
        id="gba",
        display_name="Game Boy Advance",
        short_name="GBA",
        manufacturer="Nintendo",
        generation=6,
        extensions=[".gba"],
        header_rule=None,
        libretro_name="Nintendo - Game Boy Advance",
        folder_aliases=["gba", "gameboyadvance"],
        dat_name="Nintendo - Game Boy Advance",
    ),
    SystemDef(
        id="nds",
        display_name="Nintendo DS",
        short_name="DS",
        manufacturer="Nintendo",
        generation=7,
        extensions=[".nds"],
        header_rule=None,
        libretro_name="Nintendo - Nintendo DS",
        folder_aliases=["nds", "ds"],
        dat_name="Nintendo - Nintendo DS",
        dat_name_aliases=[
            "Nintendo - Nintendo DS (Decrypted)",
            "Nintendo - Nintendo DS (Download Play)",
        ],
    ),
    SystemDef(
        id="virtualboy",
        display_name="Virtual Boy",
        short_name="VB",
        manufacturer="Nintendo",
        generation=5,
        extensions=[".vb", ".vboy"],
        header_rule=None,
        libretro_name="Nintendo - Virtual Boy",
        folder_aliases=["vb", "virtualboy"],
        dat_name="Nintendo - Virtual Boy",
    ),
    # --- Sega ---
    SystemDef(
        id="megadrive",
        display_name="Sega Mega Drive / Genesis",
        short_name="Mega Drive",
        manufacturer="Sega",
        generation=4,
        extensions=[".md", ".gen", ".bin", ".smd", ".68k"],
        header_rule=None,
        libretro_name="Sega - Mega Drive - Genesis",
        folder_aliases=["megadrive", "genesis", "md", "gen"],
        dat_name="Sega - Mega Drive - Genesis",
    ),
    SystemDef(
        id="mastersystem",
        display_name="Sega Master System",
        short_name="Master System",
        manufacturer="Sega",
        generation=3,
        extensions=[".sms", ".bin"],
        header_rule=None,
        libretro_name="Sega - Master System - Mark III",
        folder_aliases=["mastersystem", "sms"],
        dat_name="Sega - Master System - Mark III",
    ),
    SystemDef(
        id="gamegear",
        display_name="Sega Game Gear",
        short_name="Game Gear",
        manufacturer="Sega",
        generation=4,
        extensions=[".gg"],
        header_rule=None,
        libretro_name="Sega - Game Gear",
        folder_aliases=["gamegear", "gg"],
        dat_name="Sega - Game Gear",
    ),
    SystemDef(
        id="saturn",
        display_name="Sega Saturn",
        short_name="Saturn",
        manufacturer="Sega",
        generation=5,
        extensions=[".cue", ".iso", ".chd", ".mds", ".m3u", ".ccd"],
        header_rule=None,
        libretro_name="Sega - Saturn",
        folder_aliases=["saturn", "ss", "segasaturn"],
        dat_name="Sega - Saturn",
    ),
    SystemDef(
        id="dreamcast",
        display_name="Sega Dreamcast",
        short_name="Dreamcast",
        manufacturer="Sega",
        generation=6,
        extensions=[".gdi", ".cdi", ".chd", ".cue", ".m3u"],
        header_rule=None,
        libretro_name="Sega - Dreamcast",
        folder_aliases=["dreamcast", "dc"],
        dat_name="Sega - Dreamcast",
    ),
    SystemDef(
        id="sega32x",
        display_name="Sega 32X",
        short_name="32X",
        manufacturer="Sega",
        generation=4,
        extensions=[".32x", ".bin"],
        header_rule=None,
        libretro_name="Sega - 32X",
        folder_aliases=["32x", "sega32x"],
        dat_name="Sega - 32X",
    ),
    # --- Sony ---
    SystemDef(
        id="psx",
        display_name="Sony PlayStation",
        short_name="PSX",
        manufacturer="Sony",
        generation=5,
        extensions=[".cue", ".bin", ".iso", ".chd", ".pbp", ".m3u", ".ecm", ".ccd"],
        header_rule=None,
        libretro_name="Sony - PlayStation",
        folder_aliases=["psx", "ps1", "playstation", "psone"],
        dat_name="Sony - PlayStation",
    ),
    SystemDef(
        id="psp",
        display_name="Sony PlayStation Portable",
        short_name="PSP",
        manufacturer="Sony",
        generation=7,
        extensions=[".iso", ".cso", ".pbp", ".zso", ".dax"],
        header_rule=None,
        libretro_name="Sony - PlayStation Portable",
        folder_aliases=["psp"],
        dat_name="Sony - PlayStation Portable",
    ),
    # --- Atari ---
    SystemDef(
        id="atari2600",
        display_name="Atari 2600",
        short_name="2600",
        manufacturer="Atari",
        generation=2,
        extensions=[".a26", ".bin"],
        header_rule=None,
        libretro_name="Atari - 2600",
        folder_aliases=["atari2600", "a2600", "2600"],
        dat_name="Atari - 2600",
    ),
    SystemDef(
        id="atari7800",
        display_name="Atari 7800",
        short_name="7800",
        manufacturer="Atari",
        generation=3,
        extensions=[".a78", ".bin"],
        header_rule=None,
        libretro_name="Atari - 7800",
        folder_aliases=["atari7800", "a7800", "7800"],
        dat_name="Atari - 7800",
    ),
    SystemDef(
        id="lynx",
        display_name="Atari Lynx",
        short_name="Lynx",
        manufacturer="Atari",
        generation=4,
        extensions=[".lnx", ".lyx", ".o"],
        header_rule="lynx_64",
        libretro_name="Atari - Lynx",
        folder_aliases=["lynx", "atarilynx"],
        dat_name="Atari - Lynx",
    ),
    SystemDef(
        id="atarist",
        display_name="Atari ST",
        short_name="Atari ST",
        manufacturer="Atari",
        generation=None,
        extensions=[".st", ".msa", ".stx", ".dim"],
        header_rule=None,
        libretro_name="Atari - ST",
        folder_aliases=["atarist", "st"],
        dat_name="Atari - ST",
    ),
    # --- NEC ---
    SystemDef(
        id="pcengine",
        display_name="PC Engine / TurboGrafx-16",
        short_name="PCE",
        manufacturer="NEC",
        generation=4,
        extensions=[".pce", ".bin"],
        header_rule=None,
        libretro_name="NEC - PC Engine - TurboGrafx 16",
        folder_aliases=["pcengine", "tg16", "pce", "turbografx16"],
        dat_name="NEC - PC Engine - TurboGrafx 16",
    ),
    SystemDef(
        id="pcenginecd",
        display_name="PC Engine CD / TurboGrafx-CD",
        short_name="PCE-CD",
        manufacturer="NEC",
        generation=4,
        extensions=[".cue", ".bin", ".iso", ".chd", ".ccd"],
        header_rule=None,
        libretro_name="NEC - PC Engine CD - TurboGrafx-CD",
        folder_aliases=["pcenginecd", "tg-cd", "pcecd", "turbografxcd"],
        dat_name="NEC - PC Engine CD - TurboGrafx-CD",
    ),
    # --- SNK ---
    SystemDef(
        id="neogeo",
        display_name="SNK Neo Geo",
        short_name="Neo Geo",
        manufacturer="SNK",
        generation=4,
        extensions=[".zip", ".7z"],
        header_rule=None,
        libretro_name="SNK - Neo Geo",
        folder_aliases=["neogeo"],
        dat_name="SNK - Neo Geo",
    ),
    SystemDef(
        id="ngp",
        display_name="Neo Geo Pocket",
        short_name="NGP",
        manufacturer="SNK",
        generation=5,
        extensions=[".ngp"],
        header_rule=None,
        libretro_name="SNK - Neo Geo Pocket",
        folder_aliases=["ngp", "neogeopocket"],
        dat_name="SNK - Neo Geo Pocket",
    ),
    SystemDef(
        id="ngpc",
        display_name="Neo Geo Pocket Color",
        short_name="NGPC",
        manufacturer="SNK",
        generation=5,
        extensions=[".ngc", ".npc"],
        header_rule=None,
        libretro_name="SNK - Neo Geo Pocket Color",
        folder_aliases=["ngpc", "neogeopocketcolor"],
        dat_name="SNK - Neo Geo Pocket Color",
    ),
    # --- Arcade ---
    SystemDef(
        id="mame",
        display_name="MAME",
        short_name="MAME",
        manufacturer="Various",
        generation=None,
        extensions=[".zip", ".7z", ".chd"],
        header_rule=None,
        libretro_name="MAME",
        folder_aliases=["mame", "arcade"],
        dat_name="MAME",
    ),
    SystemDef(
        id="fbneo",
        display_name="FinalBurn Neo",
        short_name="FBNeo",
        manufacturer="Various",
        generation=None,
        extensions=[".zip", ".7z"],
        header_rule=None,
        libretro_name="FBNeo - Arcade Games",
        folder_aliases=["fbneo", "fba", "fbn"],
        dat_name="FBNeo - Arcade Games",
    ),
    # --- Home computers ---
    SystemDef(
        id="msx",
        display_name="MSX",
        short_name="MSX",
        manufacturer="Microsoft / ASCII",
        generation=None,
        extensions=[".rom", ".dsk", ".cas", ".mx1", ".mx2", ".m3u"],
        header_rule=None,
        libretro_name="Microsoft - MSX",
        folder_aliases=["msx", "msx1"],
        dat_name="Microsoft - MSX",
        dat_name_aliases=["Microsoft - MSX2"],
    ),
    SystemDef(
        id="amiga",
        display_name="Commodore Amiga",
        short_name="Amiga",
        manufacturer="Commodore",
        generation=None,
        extensions=[".adf", ".adz", ".ipf", ".dms", ".hdf", ".lha"],
        header_rule=None,
        libretro_name="Commodore - Amiga",
        folder_aliases=["amiga", "amiga500", "amiga1200"],
        dat_name="Commodore - Amiga",
    ),
    SystemDef(
        id="c64",
        display_name="Commodore 64",
        short_name="C64",
        manufacturer="Commodore",
        generation=None,
        extensions=[".d64", ".d71", ".d81", ".t64", ".tap", ".prg", ".crt"],
        header_rule=None,
        libretro_name="Commodore - 64",
        folder_aliases=["c64", "commodore64"],
        dat_name="Commodore - 64",
        dat_name_aliases=["Commodore - 64 (PP)", "Commodore - 64 (Tapes)"],
    ),
    SystemDef(
        id="zxspectrum",
        display_name="Sinclair ZX Spectrum",
        short_name="ZX Spectrum",
        manufacturer="Sinclair",
        generation=None,
        extensions=[".tap", ".tzx", ".sna", ".z80", ".dsk", ".trd", ".scl", ".szx"],
        header_rule=None,
        libretro_name="Sinclair - ZX Spectrum",
        folder_aliases=["zxspectrum", "zx", "spectrum"],
        dat_name="Sinclair - ZX Spectrum",
        dat_name_aliases=["Sinclair - ZX Spectrum +3"],
    ),
    SystemDef(
        id="amstradcpc",
        display_name="Amstrad CPC",
        short_name="CPC",
        manufacturer="Amstrad",
        generation=None,
        extensions=[".dsk", ".cdt", ".cpr"],
        header_rule=None,
        libretro_name="Amstrad - CPC",
        folder_aliases=["amstradcpc", "cpc"],
        dat_name="Amstrad - CPC",
    ),
]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_systems(conn: sqlite3.Connection) -> int:
    """Insert SYSTEM_REGISTRY entries into the `systems` table.

    Idempotent — uses INSERT OR IGNORE, so existing rows are left untouched.
    Returns the number of rows actually inserted (0 if already seeded).
    """
    inserted = 0
    for sys_def in SYSTEM_REGISTRY:
        inserted += conn.execute(
            """
            INSERT OR IGNORE INTO systems (
                id, display_name, short_name, manufacturer, generation,
                extensions, header_rule, libretro_name, folder_aliases, dat_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sys_def.id,
                sys_def.display_name,
                sys_def.short_name,
                sys_def.manufacturer,
                sys_def.generation,
                json.dumps(sys_def.extensions),
                sys_def.header_rule,
                sys_def.libretro_name,
                json.dumps(sys_def.folder_aliases),
                sys_def.dat_name,
            ),
        ).rowcount
    conn.commit()
    return inserted


def get_systems_by_alias(conn: sqlite3.Connection) -> dict[str, str]:
    """Build a lookup table mapping every folder alias (lowercase) to its system id.

    Used by the scanner to detect platforms from directory names. Aliases are
    stored as JSON arrays in the `systems.folder_aliases` column; this helper
    flattens them into a single dict for O(1) lookups.
    """
    rows = conn.execute("SELECT id, folder_aliases FROM systems").fetchall()
    alias_map: dict[str, str] = {}
    for row in rows:
        for alias in json.loads(row["folder_aliases"]):
            alias_map[alias.lower()] = row["id"]
    return alias_map


def get_extensions_by_system(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Build a lookup table mapping system_id to its list of accepted extensions."""
    rows = conn.execute("SELECT id, extensions FROM systems").fetchall()
    return {
        row["id"]: [ext.lower() for ext in json.loads(row["extensions"])]
        for row in rows
    }
