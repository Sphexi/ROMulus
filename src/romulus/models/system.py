"""System (platform) data model and registry.

The system registry is the source of truth for which platforms ROMulus supports.
Each entry codifies the accepted file extensions, folder-name aliases (across
RetroArch / Batocera / Anbernic / Onion / muOS / ArkOS / ROCKNIX), a header rule
used for normalization prior to hashing, and the libretro thumbnail folder name.

The registry is seeded into the `systems` SQLite table on first run. At import
time it is loaded from ``systems/builtin.yaml`` (next to the install dir, or
the in-repo copy when running from source). The hardcoded ``_FALLBACK_REGISTRY``
below stays as a safety net: if the YAML is missing or malformed we log a loud
error and use the in-code list so the app still starts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

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
    logo_dark: str | None = Field(
        default=None,
        description=(
            "Path to a logo PNG suitable for dark UI themes, relative to "
            "``src/romulus/ui/artwork/`` (e.g. ``systems/nes-dark.png``). "
            "Resolved by :func:`romulus.ui.artwork.resolve_system_logo`."
        ),
    )
    logo_light: str | None = Field(
        default=None,
        description=(
            "Path to a logo PNG suitable for light UI themes, relative to "
            "``src/romulus/ui/artwork/``."
        ),
    )
    gamedb_file: str | None = Field(
        default=None,
        description=(
            "Filename of the bundled GameDB JSON for this system "
            "(e.g. ``gba.json``), relative to ``data/gamedb/``. Provides "
            "an offline first-pass metadata source — see "
            ":mod:`romulus.metadata.gamedb`. Sourced from "
            "https://github.com/niemasd/GameDB."
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

_FALLBACK_REGISTRY: list[SystemDef] = [
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
        logo_dark="systems/nes-dark.png",
        logo_light="systems/nes-light.png",
        gamedb_file="nes.json",
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
        logo_dark="systems/snes-dark.png",
        logo_light="systems/snes-light.png",
        gamedb_file="snes.json",
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
        logo_dark="systems/n64-dark.png",
        logo_light="systems/n64-light.png",
        gamedb_file="n64.json",
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
        logo_dark="systems/gamecube-dark.png",
        logo_light="systems/gamecube-light.png",
        gamedb_file="gamecube.json",
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
        logo_dark="systems/gb-dark.png",
        logo_light="systems/gb-light.png",
        gamedb_file="gb.json",
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
        logo_dark="systems/gbc-dark.png",
        logo_light="systems/gbc-light.png",
        gamedb_file="gbc.json",
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
        logo_dark="systems/gba-dark.png",
        logo_light="systems/gba-light.png",
        gamedb_file="gba.json",
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
            # DSi cartridge dumps share the DS cart slot and use the same
            # filesystem layout. Treat DSi (Decrypted) carts as the same
            # logical system as DS for v0.1.0 — DSi-exclusive titles are
            # rare and the user can still route them via folder aliases.
            "Nintendo - Nintendo DSi (Decrypted)",
        ],
        logo_dark="systems/nds-dark.png",
        logo_light="systems/nds-light.png",
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
        logo_dark="systems/virtualboy-dark.png",
        logo_light="systems/virtualboy-light.png",
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
        logo_dark="systems/megadrive-dark.png",
        logo_light="systems/megadrive-light.png",
        gamedb_file="megadrive.json",
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
        logo_dark="systems/mastersystem-dark.png",
        logo_light="systems/mastersystem-light.png",
        gamedb_file="mastersystem.json",
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
        logo_dark="systems/gamegear-dark.png",
        logo_light="systems/gamegear-light.png",
        gamedb_file="gamegear.json",
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
        logo_dark="systems/saturn-dark.png",
        logo_light="systems/saturn-light.png",
        gamedb_file="saturn.json",
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
        logo_dark="systems/dreamcast-dark.png",
        logo_light="systems/dreamcast-light.png",
        gamedb_file="dreamcast.json",
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
        logo_dark="systems/sega32x-dark.png",
        logo_light="systems/sega32x-light.png",
        gamedb_file="sega32x.json",
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
        logo_dark="systems/psx-dark.png",
        logo_light="systems/psx-light.png",
        gamedb_file="psx.json",
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
        dat_name_aliases=[
            # PSN downloadable releases — same logical system, different
            # delivery channel. PPSSPP loads decrypted PSN dumps directly;
            # encrypted ones can be decrypted with user-supplied keys.
            "Sony - PlayStation Portable (PSN) (Decrypted)",
            "Sony - PlayStation Portable (PSN) (Encrypted)",
            # PSX2PSP wrappers re-package PS1 discs inside a PSP EBOOT. The
            # bundled libretro PPSSPP core handles them.
            "Sony - PlayStation Portable (PSX2PSP)",
        ],
        logo_dark="systems/psp-dark.png",
        logo_light="systems/psp-light.png",
        gamedb_file="psp.json",
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
        logo_dark="systems/atari2600-dark.png",
        logo_light="systems/atari2600-light.png",
        gamedb_file="atari2600.json",
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
        logo_dark="systems/atari7800-dark.png",
        logo_light="systems/atari7800-light.png",
        gamedb_file="atari7800.json",
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
        logo_dark="systems/lynx-dark.png",
        logo_light="systems/lynx-light.png",
        gamedb_file="lynx.json",
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
        logo_dark="systems/atarist-dark.png",
        logo_light="systems/atarist-light.png",
        gamedb_file="atarist.json",
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
        logo_dark="systems/pcengine-dark.png",
        logo_light="systems/pcengine-light.png",
        gamedb_file="pcengine.json",
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
        logo_dark="systems/pcenginecd-dark.png",
        logo_light="systems/pcenginecd-light.png",
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
        logo_dark="systems/neogeo-dark.png",
        logo_light="systems/neogeo-light.png",
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
        logo_dark="systems/ngp-dark.png",
        logo_light="systems/ngp-light.png",
        gamedb_file="ngp.json",
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
        logo_dark="systems/ngpc-dark.png",
        logo_light="systems/ngpc-light.png",
        gamedb_file="ngpc.json",
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
        logo_dark="systems/mame-dark.png",
        logo_light="systems/mame-light.png",
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
        logo_dark="systems/fbneo-dark.png",
        logo_light="systems/fbneo-light.png",
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
        logo_dark="systems/msx-dark.png",
        logo_light="systems/msx-light.png",
        gamedb_file="msx.json",
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
        logo_dark="systems/amiga-dark.png",
        logo_light="systems/amiga-light.png",
        gamedb_file="amiga.json",
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
        logo_dark="systems/c64-dark.png",
        logo_light="systems/c64-light.png",
        gamedb_file="c64.json",
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
        logo_dark="systems/zxspectrum-dark.png",
        logo_light="systems/zxspectrum-light.png",
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
        logo_dark="systems/amstradcpc-dark.png",
        logo_light="systems/amstradcpc-light.png",
    ),
    # --- Atari (extended) ---
    SystemDef(
        id="atari5200",
        display_name="Atari 5200",
        short_name="5200",
        manufacturer="Atari",
        generation=2,
        extensions=[".a52", ".bin", ".rom"],
        header_rule=None,
        libretro_name="Atari - 5200",
        folder_aliases=["atari5200", "a5200", "5200"],
        dat_name="Atari - 5200",
        logo_dark="systems/atari5200-dark.png",
        logo_light="systems/atari5200-light.png",
        gamedb_file="atari5200.json",
    ),
    SystemDef(
        id="jaguar",
        display_name="Atari Jaguar",
        short_name="Jaguar",
        manufacturer="Atari",
        generation=5,
        extensions=[".j64", ".jag", ".rom", ".bin"],
        header_rule=None,
        libretro_name="Atari - Jaguar",
        folder_aliases=["jaguar", "atarijaguar"],
        dat_name="Atari - Jaguar (J64)",
        logo_dark="systems/jaguar-dark.png",
        logo_light="systems/jaguar-light.png",
        gamedb_file="jaguar.json",
    ),
    # --- Bandai ---
    SystemDef(
        id="wonderswan",
        display_name="Bandai WonderSwan",
        short_name="WS",
        manufacturer="Bandai",
        generation=5,
        extensions=[".ws"],
        header_rule=None,
        libretro_name="Bandai - WonderSwan",
        folder_aliases=["wonderswan", "ws", "wswan"],
        dat_name="Bandai - WonderSwan",
        logo_dark="systems/wonderswan-dark.png",
        logo_light="systems/wonderswan-light.png",
        gamedb_file="wonderswan.json",
    ),
    SystemDef(
        id="wonderswancolor",
        display_name="Bandai WonderSwan Color",
        short_name="WSC",
        manufacturer="Bandai",
        generation=5,
        extensions=[".wsc", ".ws"],
        header_rule=None,
        libretro_name="Bandai - WonderSwan Color",
        folder_aliases=["wonderswancolor", "wsc", "wswanc"],
        dat_name="Bandai - WonderSwan Color",
        logo_dark="systems/wonderswancolor-dark.png",
        logo_light="systems/wonderswancolor-light.png",
        gamedb_file="wonderswancolor.json",
    ),
    # --- Benesse ---
    # Pocket Challenge V2 is a WonderSwan-compatible educational handheld sold
    # by Benesse in Japan. Its cartridges are physically and electrically the
    # same as WonderSwan carts, but ship under a distinct No-Intro header so
    # the DAT routes here rather than to ``wonderswan``.
    SystemDef(
        id="pocketchallengev2",
        display_name="Benesse Pocket Challenge V2",
        short_name="PC V2",
        manufacturer="Benesse",
        generation=5,
        extensions=[".pc2", ".ws", ".bin"],
        header_rule=None,
        libretro_name="Benesse - Pocket Challenge V2",
        folder_aliases=["pocketchallengev2", "pcv2"],
        dat_name="Benesse - Pocket Challenge V2",
    ),
    # --- Coleco ---
    SystemDef(
        id="colecovision",
        display_name="ColecoVision",
        short_name="ColecoVision",
        manufacturer="Coleco",
        generation=2,
        extensions=[".col", ".bin", ".rom"],
        header_rule=None,
        libretro_name="Coleco - ColecoVision",
        folder_aliases=["colecovision", "coleco"],
        dat_name="Coleco - ColecoVision",
        logo_dark="systems/colecovision-dark.png",
        logo_light="systems/colecovision-light.png",
        gamedb_file="colecovision.json",
    ),
    # --- Commodore (extended) ---
    SystemDef(
        id="c64plus4",
        display_name="Commodore Plus/4",
        short_name="Plus/4",
        manufacturer="Commodore",
        generation=None,
        extensions=[".prg", ".d64", ".t64", ".tap", ".crt"],
        header_rule=None,
        libretro_name="Commodore - Plus-4",
        folder_aliases=["plus4", "c16", "commodoreplus4"],
        dat_name="Commodore - Plus-4",
        logo_dark="systems/c64plus4-dark.png",
        logo_light="systems/c64plus4-light.png",
    ),
    SystemDef(
        id="vic20",
        display_name="Commodore VIC-20",
        short_name="VIC-20",
        manufacturer="Commodore",
        generation=None,
        extensions=[".prg", ".crt", ".t64", ".tap", ".d64"],
        header_rule=None,
        libretro_name="Commodore - VIC-20",
        folder_aliases=["vic20", "vic-20", "commodorevic20"],
        dat_name="Commodore - VIC-20",
        logo_dark="systems/vic20-dark.png",
        logo_light="systems/vic20-light.png",
    ),
    # --- Other classic / mini consoles ---
    SystemDef(
        id="arcadia2001",
        display_name="Emerson Arcadia 2001",
        short_name="Arcadia 2001",
        manufacturer="Emerson",
        generation=2,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Emerson - Arcadia 2001",
        folder_aliases=["arcadia", "arcadia2001"],
        dat_name="Emerson - Arcadia 2001",
        logo_dark="systems/arcadia2001-dark.png",
        logo_light="systems/arcadia2001-light.png",
    ),
    SystemDef(
        id="adventurevision",
        display_name="Entex Adventure Vision",
        short_name="Adventure Vision",
        manufacturer="Entex",
        generation=2,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Entex - Adventure Vision",
        folder_aliases=["adventurevision"],
        dat_name="Entex - Adventure Vision",
        logo_dark="systems/adventurevision-dark.png",
        logo_light="systems/adventurevision-light.png",
    ),
    SystemDef(
        id="scv",
        display_name="Epoch Super Cassette Vision",
        short_name="SCV",
        manufacturer="Epoch",
        generation=3,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Epoch - Super Cassette Vision",
        folder_aliases=["scv", "supercassettevision"],
        dat_name="Epoch - Super Cassette Vision",
        logo_dark="systems/scv-dark.png",
        logo_light="systems/scv-light.png",
    ),
    SystemDef(
        id="channelf",
        display_name="Fairchild Channel F",
        short_name="Channel F",
        manufacturer="Fairchild",
        generation=2,
        extensions=[".bin", ".chf", ".rom"],
        header_rule=None,
        libretro_name="Fairchild - Channel F",
        folder_aliases=["channelf", "chanf"],
        dat_name="Fairchild - Channel F",
        logo_dark="systems/channelf-dark.png",
        logo_light="systems/channelf-light.png",
    ),
    SystemDef(
        id="superacan",
        display_name="Funtech Super A'Can",
        short_name="Super A'Can",
        manufacturer="Funtech",
        generation=4,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Funtech - Super Acan",
        folder_aliases=["superacan"],
        dat_name="Funtech - Super Acan",
        logo_dark="systems/superacan-dark.png",
        logo_light="systems/superacan-light.png",
    ),
    SystemDef(
        id="vectrex",
        display_name="GCE Vectrex",
        short_name="Vectrex",
        manufacturer="GCE",
        generation=2,
        extensions=[".vec", ".bin", ".gam"],
        header_rule=None,
        libretro_name="GCE - Vectrex",
        folder_aliases=["vectrex"],
        dat_name="GCE - Vectrex",
        logo_dark="systems/vectrex-dark.png",
        logo_light="systems/vectrex-light.png",
        gamedb_file="vectrex.json",
    ),
    SystemDef(
        id="gamemaster",
        display_name="Hartung Game Master",
        short_name="Game Master",
        manufacturer="Hartung",
        generation=4,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Hartung - Game Master",
        folder_aliases=["gamemaster", "hartung"],
        dat_name="Hartung - Game Master",
        logo_dark="systems/gamemaster-dark.png",
        logo_light="systems/gamemaster-light.png",
    ),
    # --- Konami ---
    # The Picno was a Japan-only edutainment console aimed at toddlers. Tiny
    # library, but the DAT exists so the SystemDef does too.
    SystemDef(
        id="picno",
        display_name="Konami Picno",
        short_name="Picno",
        manufacturer="Konami",
        generation=4,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Konami - Picno",
        folder_aliases=["picno"],
        dat_name="Konami - Picno",
    ),
    # --- LeapFrog (educational) ---
    # LeapFrog's edutainment hardware. Cartridges aren't really games in the
    # traditional sense, but No-Intro publishes DATs for them so ROMulus
    # recognizes the platforms for users who collect them.
    SystemDef(
        id="leappad",
        display_name="LeapFrog LeapPad",
        short_name="LeapPad",
        manufacturer="LeapFrog",
        generation=None,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="LeapFrog - LeapPad",
        folder_aliases=["leappad"],
        dat_name="LeapFrog - LeapPad",
    ),
    SystemDef(
        id="leapster",
        display_name="LeapFrog Leapster",
        short_name="Leapster",
        manufacturer="LeapFrog",
        generation=None,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="LeapFrog - Leapster Learning Game System",
        folder_aliases=["leapster", "leapstergls"],
        dat_name="LeapFrog - Leapster Learning Game System",
    ),
    SystemDef(
        id="myfirstleappad",
        display_name="LeapFrog My First LeapPad",
        short_name="My First LeapPad",
        manufacturer="LeapFrog",
        generation=None,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="LeapFrog - My First LeapPad",
        folder_aliases=["myfirstleappad"],
        dat_name="LeapFrog - My First LeapPad",
    ),
    # Magnavox Odyssey 2 and Philips Videopac+ are the same physical hardware
    # (PAL Europe sold it as Videopac, Videopac+ added the G7400 graphics chip).
    # The Videopac+ DAT is aliased onto this single SystemDef rather than
    # spawning a duplicate registry entry.
    SystemDef(
        id="odyssey2",
        display_name="Magnavox Odyssey 2",
        short_name="Odyssey 2",
        manufacturer="Magnavox",
        generation=2,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Magnavox - Odyssey2",
        folder_aliases=["odyssey2", "o2em", "videopac", "odyssey"],
        dat_name="Magnavox - Odyssey2",
        dat_name_aliases=["Philips - Videopac+"],
        logo_dark="systems/odyssey2-dark.png",
        logo_light="systems/odyssey2-light.png",
    ),
    SystemDef(
        id="intellivision",
        display_name="Mattel Intellivision",
        short_name="Intellivision",
        manufacturer="Mattel",
        generation=2,
        extensions=[".int", ".bin", ".rom", ".itv"],
        header_rule=None,
        libretro_name="Mattel - Intellivision",
        folder_aliases=["intellivision", "intv"],
        dat_name="Mattel - Intellivision",
        logo_dark="systems/intellivision-dark.png",
        logo_light="systems/intellivision-light.png",
    ),
    SystemDef(
        id="studio2",
        display_name="RCA Studio II",
        short_name="Studio II",
        manufacturer="RCA",
        generation=2,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="RCA - Studio II",
        folder_aliases=["studio2", "rcastudio2"],
        dat_name="RCA - Studio II",
        logo_dark="systems/studio2-dark.png",
        logo_light="systems/studio2-light.png",
    ),
    SystemDef(
        id="gamecom",
        display_name="Tiger Game.com",
        short_name="Game.com",
        manufacturer="Tiger",
        generation=5,
        extensions=[".tgc", ".bin"],
        header_rule=None,
        libretro_name="Tiger - Game.com",
        folder_aliases=["gamecom", "game.com"],
        dat_name="Tiger - Game.com",
        logo_dark="systems/gamecom-dark.png",
        logo_light="systems/gamecom-light.png",
        gamedb_file="gamecom.json",
    ),
    SystemDef(
        id="creativision",
        display_name="VTech CreatiVision",
        short_name="CreatiVision",
        manufacturer="VTech",
        generation=2,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="VTech - CreatiVision",
        folder_aliases=["creativision", "vtechcreativision"],
        dat_name="VTech - CreatiVision",
        logo_dark="systems/creativision-dark.png",
        logo_light="systems/creativision-light.png",
    ),
    SystemDef(
        id="vsmile",
        display_name="VTech V.Smile",
        short_name="V.Smile",
        manufacturer="VTech",
        generation=6,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="VTech - V.Smile",
        folder_aliases=["vsmile"],
        dat_name="VTech - V.Smile",
    ),
    SystemDef(
        id="supervision",
        display_name="Watara Supervision",
        short_name="Supervision",
        manufacturer="Watara",
        generation=4,
        extensions=[".sv", ".bin"],
        header_rule=None,
        libretro_name="Watara - Supervision",
        folder_aliases=["supervision", "watara"],
        dat_name="Watara - Supervision",
        logo_dark="systems/supervision-dark.png",
        logo_light="systems/supervision-light.png",
        gamedb_file="supervision.json",
    ),
    # --- NEC (extended) ---
    SystemDef(
        id="supergrafx",
        display_name="NEC PC Engine SuperGrafx",
        short_name="SuperGrafx",
        manufacturer="NEC",
        generation=4,
        extensions=[".sgx", ".pce"],
        header_rule=None,
        libretro_name="NEC - PC Engine SuperGrafx",
        folder_aliases=["pcenginesgx", "sgx", "supergrafx"],
        dat_name="NEC - PC Engine SuperGrafx",
        logo_dark="systems/supergrafx-dark.png",
        logo_light="systems/supergrafx-light.png",
        gamedb_file="supergrafx.json",
    ),
    # --- Sega (extended) ---
    SystemDef(
        id="sg1000",
        display_name="Sega SG-1000",
        short_name="SG-1000",
        manufacturer="Sega",
        generation=3,
        extensions=[".sg", ".bin", ".rom"],
        header_rule=None,
        libretro_name="Sega - SG-1000",
        folder_aliases=["sg-1000", "sg1000"],
        dat_name="Sega - SG-1000",
        logo_dark="systems/sg1000-dark.png",
        logo_light="systems/sg1000-light.png",
        gamedb_file="sg1000.json",
    ),
    SystemDef(
        id="segapico",
        display_name="Sega Pico",
        short_name="Pico",
        manufacturer="Sega",
        generation=4,
        extensions=[".bin", ".md"],
        header_rule=None,
        libretro_name="Sega - PICO",
        folder_aliases=["pico", "segapico"],
        dat_name="Sega - PICO",
        logo_dark="systems/segapico-dark.png",
        logo_light="systems/segapico-light.png",
        gamedb_file="segapico.json",
    ),
    # The Beena was a Japan-only educational console aimed at preschoolers,
    # released by Sega in 2005. Routed for completeness; no real cores emulate
    # it today.
    SystemDef(
        id="beena",
        display_name="Sega Beena",
        short_name="Beena",
        manufacturer="Sega",
        generation=7,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Sega - Beena",
        folder_aliases=["beena"],
        dat_name="Sega - Beena",
    ),
    # --- Nintendo extensions / accessories ---
    SystemDef(
        id="n64dd",
        display_name="Nintendo 64DD",
        short_name="64DD",
        manufacturer="Nintendo",
        generation=5,
        extensions=[".ndd"],
        header_rule=None,
        libretro_name="Nintendo - Nintendo 64DD",
        folder_aliases=["n64dd", "64dd"],
        dat_name="Nintendo - Nintendo 64DD",
        logo_dark="systems/n64dd-dark.png",
        logo_light="systems/n64dd-light.png",
        gamedb_file="n64dd.json",
    ),
    SystemDef(
        id="pokemini",
        display_name="Pokemon Mini",
        short_name="Pokemini",
        manufacturer="Nintendo",
        generation=6,
        extensions=[".min"],
        header_rule=None,
        libretro_name="Nintendo - Pokemon Mini",
        folder_aliases=["pokemini", "pmini"],
        dat_name="Nintendo - Pokemon Mini",
        logo_dark="systems/pokemini-dark.png",
        logo_light="systems/pokemini-light.png",
    ),
    SystemDef(
        id="satellaview",
        display_name="Nintendo Satellaview",
        short_name="Satellaview",
        manufacturer="Nintendo",
        generation=4,
        extensions=[".bs", ".sfc"],
        header_rule=None,
        libretro_name="Nintendo - Satellaview",
        folder_aliases=["satellaview", "bsx"],
        dat_name="Nintendo - Satellaview",
        logo_dark="systems/satellaview-dark.png",
        logo_light="systems/satellaview-light.png",
        gamedb_file="satellaview.json",
    ),
    # Sufami Turbo: SNES cartridge accessory. The `.st` extension collides with
    # Atari ST disk images — disambiguation has to come from folder/system
    # context at scan time, not extension alone.
    SystemDef(
        id="sufami",
        display_name="Nintendo Sufami Turbo",
        short_name="Sufami Turbo",
        manufacturer="Nintendo",
        generation=4,
        extensions=[".st", ".sfc"],
        header_rule=None,
        libretro_name="Nintendo - Sufami Turbo",
        folder_aliases=["sufami"],
        dat_name="Nintendo - Sufami Turbo",
        logo_dark="systems/sufami-dark.png",
        logo_light="systems/sufami-light.png",
        gamedb_file="sufami.json",
    ),
    SystemDef(
        id="ereader",
        display_name="Nintendo e-Reader",
        short_name="e-Reader",
        manufacturer="Nintendo",
        generation=6,
        extensions=[".bin", ".raw"],
        header_rule=None,
        libretro_name="Nintendo - e-Reader",
        folder_aliases=["ereader", "e-reader"],
        dat_name="Nintendo - e-Reader",
        logo_dark="systems/ereader-dark.png",
        logo_light="systems/ereader-light.png",
    ),
    # --- Korean / Japanese niche ---
    SystemDef(
        id="gp32",
        display_name="GamePark GP32",
        short_name="GP32",
        manufacturer="GamePark",
        generation=6,
        extensions=[".gpk", ".smc"],
        header_rule=None,
        libretro_name="GamePark - GP32",
        folder_aliases=["gp32", "gameparkgp32"],
        dat_name="GamePark - GP32",
    ),
    SystemDef(
        id="casioloopy",
        display_name="Casio Loopy",
        short_name="Loopy",
        manufacturer="Casio",
        generation=5,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Casio - Loopy",
        folder_aliases=["loopy", "casioloopy"],
        dat_name="Casio - Loopy",
        logo_dark="systems/casioloopy-dark.png",
        logo_light="systems/casioloopy-light.png",
        gamedb_file="casioloopy.json",
    ),
    SystemDef(
        id="pv1000",
        display_name="Casio PV-1000",
        short_name="PV-1000",
        manufacturer="Casio",
        generation=2,
        extensions=[".bin"],
        header_rule=None,
        libretro_name="Casio - PV-1000",
        folder_aliases=["pv1000", "pv-1000"],
        dat_name="Casio - PV-1000",
        logo_dark="systems/pv1000-dark.png",
        logo_light="systems/pv1000-light.png",
    ),
    # --- Digital-distribution / install-package era ---
    #
    # These platforms predate or post-date the cartridge era. Their No-Intro
    # DATs catalog digital installs (eShop, PSN, Xbox Live, WiiWare/VC) rather
    # than original-disc dumps, so the primary ``dat_name`` is set to the
    # canonical retail header and the digital storefront variants are listed
    # as ``dat_name_aliases``. ROMulus organizes the files for the user; the
    # user is responsible for any decryption keys required by the target
    # emulator (Dolphin, Cemu, Citra, RPCS3, vita3k, etc.).
    SystemDef(
        id="wii",
        display_name="Nintendo Wii",
        short_name="Wii",
        manufacturer="Nintendo",
        generation=7,
        extensions=[".iso", ".wbfs", ".rvz", ".wia", ".ciso", ".gcz", ".wad", ".nkit.iso"],
        header_rule=None,
        libretro_name="Nintendo - Wii",
        folder_aliases=["wii"],
        # No "Nintendo - Wii" disc DAT ships in No-Intro's set (Wii retail
        # discs are catalogued by Redump). The bundled DATs are the WiiWare
        # (.wad) and CDN install dumps; both route here.
        dat_name="Nintendo - Wii",
        dat_name_aliases=[
            "Nintendo - Wii (Digital) (CDN)",
            "Nintendo - Wii (Digital) (WAD)",
        ],
        logo_dark="systems/wii-dark.png",
        logo_light="systems/wii-light.png",
        gamedb_file="wii.json",
    ),
    SystemDef(
        id="wiiu",
        display_name="Nintendo Wii U",
        short_name="Wii U",
        manufacturer="Nintendo",
        generation=8,
        extensions=[".wud", ".wux", ".wua"],
        header_rule=None,
        libretro_name="Nintendo - Wii U",
        folder_aliases=["wiiu", "wii_u", "wii-u"],
        dat_name="Nintendo - Wii U",
        dat_name_aliases=[
            "Nintendo - Wii U (Digital)",
            "Nintendo - Wii U (Digital) (CDN)",
        ],
        logo_dark="systems/wiiu-dark.png",
        logo_light="systems/wiiu-light.png",
    ),
    SystemDef(
        id="n3ds",
        display_name="Nintendo 3DS",
        short_name="3DS",
        manufacturer="Nintendo",
        generation=8,
        extensions=[".3ds", ".cia", ".cci", ".cxi", ".app"],
        header_rule=None,
        libretro_name="Nintendo - Nintendo 3DS",
        folder_aliases=["3ds", "n3ds", "nintendo3ds"],
        dat_name="Nintendo - Nintendo 3DS",
        # No-Intro splits 3DS into retail-encrypted, decrypted, eShop, and
        # New-3DS-exclusive variants. The CDN file even has a literal double
        # "(CDN) (CDN)" in its header — preserved verbatim.
        dat_name_aliases=[
            "Nintendo - Nintendo 3DS (Digital)",
            "Nintendo - Nintendo 3DS (Digital) (CDN) (CDN)",
            "Nintendo - Nintendo 3DS (Encrypted)",
            "Nintendo - New Nintendo 3DS (Digital)",
            "Nintendo - New Nintendo 3DS (Encrypted)",
        ],
        logo_dark="systems/n3ds-dark.png",
        logo_light="systems/n3ds-light.png",
    ),
    SystemDef(
        id="dsiware",
        display_name="Nintendo DSiWare",
        short_name="DSiWare",
        manufacturer="Nintendo",
        generation=7,
        extensions=[".nds", ".tad", ".bin"],
        header_rule=None,
        libretro_name="Nintendo - Nintendo DSi",
        folder_aliases=["dsiware", "dsi"],
        # DSi cartridge dumps (``Nintendo - Nintendo DSi (Decrypted)``) are
        # aliased onto ``nds`` because they use the same cart slot and
        # filesystem. DSiWare (digital-only shop titles) is its own thing:
        # melonDS / mGBA handle them as ``.nds`` or ``.tad`` blobs.
        dat_name="Nintendo - Nintendo DSi (Digital)",
        logo_dark="systems/dsiware-dark.png",
        logo_light="systems/dsiware-light.png",
    ),
    SystemDef(
        id="psvita",
        display_name="Sony PlayStation Vita",
        short_name="Vita",
        manufacturer="Sony",
        generation=8,
        extensions=[".vpk", ".pkg", ".mai"],
        header_rule=None,
        libretro_name="Sony - PlayStation Vita",
        folder_aliases=["psvita", "vita"],
        # ``(VPK)`` is the homebrew/packaged-install format used by vita3k.
        # The PSN variants require keys; ROMulus stores them either way.
        dat_name="Sony - PlayStation Vita (VPK)",
        dat_name_aliases=[
            "Sony - PlayStation Vita (PSN) (Decrypted)",
            "Sony - PlayStation Vita (PSN) (Encrypted)",
        ],
        logo_dark="systems/psvita-dark.png",
        logo_light="systems/psvita-light.png",
    ),
    SystemDef(
        id="ps3",
        display_name="Sony PlayStation 3",
        short_name="PS3",
        manufacturer="Sony",
        generation=7,
        extensions=[".iso", ".pkg", ".rap"],
        header_rule=None,
        libretro_name="Sony - PlayStation 3",
        folder_aliases=["ps3", "playstation3"],
        # No retail-disc PS3 DAT ships in No-Intro (Redump covers those);
        # the bundled DATs are the PSN packages. RPCS3 reads decrypted
        # dumps; encrypted ones need keys.
        dat_name="Sony - PlayStation 3 (PSN) (Decrypted)",
        dat_name_aliases=[
            "Sony - PlayStation 3 (PSN) (Encrypted)",
        ],
        logo_dark="systems/ps3-dark.png",
        logo_light="systems/ps3-light.png",
    ),
    SystemDef(
        id="xbox360",
        display_name="Microsoft Xbox 360",
        short_name="X360",
        manufacturer="Microsoft",
        generation=7,
        extensions=[".iso", ".god", ".xex"],
        header_rule=None,
        libretro_name="Microsoft - Xbox 360",
        folder_aliases=["xbox360", "x360"],
        dat_name="Microsoft - XBOX 360 (Digital)",
        dat_name_aliases=[
            "Microsoft - XBOX 360 (Title Updates) (Discontinued)",
        ],
        logo_dark="systems/xbox360-dark.png",
        logo_light="systems/xbox360-light.png",
        gamedb_file="xbox360.json",
    ),
    # --- Mobile / PDA ---
    # J2ME and Palm OS predate or sit alongside the cartridge era. There are
    # working emulators (FreeJ2ME, PHEM/MicroEmulator for J2ME; Mu / pocketsim
    # for Palm). Symbian and Zeebo have no usable emulator in 2026 and remain
    # unmapped below.
    SystemDef(
        id="j2me",
        display_name="Java ME (Mobile)",
        short_name="J2ME",
        manufacturer="Sun/Oracle",
        generation=None,
        extensions=[".jar", ".jad"],
        header_rule=None,
        libretro_name="Mobile - J2ME",
        folder_aliases=["j2me", "javame"],
        dat_name="Mobile - J2ME",
    ),
    SystemDef(
        id="palmos",
        display_name="Palm OS",
        short_name="Palm",
        manufacturer="Palm",
        generation=None,
        extensions=[".prc", ".pdb", ".pqa"],
        header_rule=None,
        libretro_name="Mobile - Palm OS",
        folder_aliases=["palm", "palmos"],
        dat_name="Mobile - Palm OS",
    ),
]


# ---------------------------------------------------------------------------
# Intentionally unmapped No-Intro DATs
#
# The following DATs ship in ``data/dats/`` for completeness — so the bundled
# DAT directory mirrors the upstream No-Intro set — but deliberately have no
# ``SystemDef`` entry. They split into the following categories:
#
# 1. IBM PC / Compatibles (all storefronts: GOG, Steam, Humble Bundle,
#    itch.io, Desura, GamersGate, MacGameStore, Microsoft Store, Misc).
#    These are not consoles. DOS/Windows games have completely different
#    identification semantics from cartridge ROMs (installers, multi-file
#    distributions, DRM wrappers, patches). A future v0.2.0+ DOSBox /
#    Windows game-library expansion would route these by adding SystemDef
#    entries above.
#
# 2. Mobile platforms with no usable emulator path — ``Mobile - Symbian``
#    and ``Mobile - Zeebo``. J2ME and Palm OS DO have working emulators
#    (FreeJ2ME, Mu) and are mapped above. Symbian's emulation story is
#    still essentially nonexistent in 2026; Zeebo titles aren't dumpable
#    from the original DRM-bound delivery network.
#
# 3. Sony - PlayStation 4 (PSN) (Encrypted) — shadPS4 is still immature
#    and decrypted PS4 dumps aren't well-distributed. Defer to v0.2.0+.
#
# 4. ``Sony - PlayStation Portable (UMD Music)`` and ``(UMD Video)`` —
#    these are PSP UMD audio and movie discs, not games. The PSP cart /
#    eboot DATs are mapped above; UMD media isn't a ROM-management
#    concern.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


class _DuplicateSystemIdError(ValueError):
    """Raised when two YAML entries claim the same system id."""


def load_systems_from_yaml(yaml_paths: Iterable[Path | str]) -> list[SystemDef]:
    """Load and validate system definitions from one or more YAML sources.

    ``yaml_paths`` may contain individual ``.yaml`` files or directories — in
    the directory case every ``*.yaml`` inside it is loaded. Each file must be
    a mapping with a top-level ``systems:`` sequence; each entry is validated
    against :class:`SystemDef`. Duplicate ids across files raise
    ``_DuplicateSystemIdError`` so a user-supplied YAML can't silently shadow
    a bundled one.

    Returns the combined list in load order (sorted file order within each
    directory, plus a stable iteration over ``yaml_paths``).
    """
    seen_ids: dict[str, Path] = {}
    out: list[SystemDef] = []
    for entry in yaml_paths:
        path = Path(entry)
        if path.is_dir():
            files = sorted(path.glob("*.yaml"))
        elif path.is_file():
            files = [path]
        else:
            continue
        for file_path in files:
            with file_path.open("r", encoding="utf-8") as fh:
                data: Any = yaml.safe_load(fh)
            if data is None:
                continue
            if not isinstance(data, dict) or "systems" not in data:
                raise ValueError(
                    f"{file_path}: top-level must be a mapping with a "
                    f"'systems:' sequence"
                )
            systems_block = data["systems"]
            if not isinstance(systems_block, list):
                raise ValueError(
                    f"{file_path}: 'systems' must be a list of entries"
                )
            for raw in systems_block:
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"{file_path}: each system entry must be a mapping, "
                        f"got {type(raw).__name__}"
                    )
                try:
                    sys_def = SystemDef.model_validate(raw)
                except ValidationError as exc:
                    raise ValueError(
                        f"{file_path}: invalid system entry "
                        f"id={raw.get('id')!r}: {exc}"
                    ) from exc
                if sys_def.id in seen_ids:
                    raise _DuplicateSystemIdError(
                        f"system id {sys_def.id!r} is defined in both "
                        f"{seen_ids[sys_def.id]} and {file_path}"
                    )
                seen_ids[sys_def.id] = file_path
                out.append(sys_def)
    return out


def _resolve_bundled_systems_dir() -> Path | None:
    """Locate the ``systems/`` directory shipped with this ROMulus install.

    Three strategies, tried in order — mirroring ``app._resolve_install_dir``
    but without importing from ``romulus.app`` (this module is imported
    earlier in the bootstrap, and importing app here would create a cycle):

    1. PyInstaller-frozen exe -> ``<exe parent>/systems``.
    2. Editable / dev clone   -> walk up from this file for ``pyproject.toml``
       and return ``<repo root>/systems``.
    3. Fall back to ``None``  -> caller uses ``_FALLBACK_REGISTRY``.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent / "systems"
        return candidate if candidate.is_dir() else None
    cursor = Path(__file__).resolve()
    for parent in cursor.parents:
        if (parent / "pyproject.toml").is_file():
            candidate = parent / "systems"
            return candidate if candidate.is_dir() else None
    return None


def _initial_registry() -> list[SystemDef]:
    """Pick the live registry: bundled YAML if present, hardcoded fallback otherwise."""
    bundled_dir = _resolve_bundled_systems_dir()
    if bundled_dir is None:
        logger.info(
            "no systems/ directory found, using hardcoded _FALLBACK_REGISTRY "
            "(%d systems)",
            len(_FALLBACK_REGISTRY),
        )
        return list(_FALLBACK_REGISTRY)
    try:
        loaded = load_systems_from_yaml([bundled_dir])
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.error(
            "failed to load systems/*.yaml from %s: %s — using hardcoded "
            "fallback (%d systems)",
            bundled_dir,
            exc,
            len(_FALLBACK_REGISTRY),
        )
        return list(_FALLBACK_REGISTRY)
    if not loaded:
        logger.warning(
            "systems/ directory %s contained no system entries — using "
            "hardcoded fallback (%d systems)",
            bundled_dir,
            len(_FALLBACK_REGISTRY),
        )
        return list(_FALLBACK_REGISTRY)
    return loaded


#: The live system registry. Populated from ``systems/*.yaml`` at import time,
#: or from ``_FALLBACK_REGISTRY`` if the YAML load fails. Reassigned in-place
#: by :func:`reload_registry` so callers that have already grabbed the binding
#: pick up the new list.
SYSTEM_REGISTRY: list[SystemDef] = _initial_registry()


def reload_registry(yaml_paths: Iterable[Path | str] | None = None) -> int:
    """Re-populate ``SYSTEM_REGISTRY`` from disk.

    With no arguments, reloads from the bundled ``systems/`` directory. Pass
    explicit paths to load from a user-controlled directory (or a mix). The
    function clears the module-level list in place and returns the new entry
    count. On any error the list is left untouched and the error is re-raised
    — callers that need graceful failure should wrap the call themselves.
    """
    if yaml_paths is None:
        bundled_dir = _resolve_bundled_systems_dir()
        if bundled_dir is None:
            raise FileNotFoundError("no bundled systems/ directory found")
        loaded = load_systems_from_yaml([bundled_dir])
    else:
        loaded = load_systems_from_yaml(yaml_paths)
    SYSTEM_REGISTRY.clear()
    SYSTEM_REGISTRY.extend(loaded)
    return len(SYSTEM_REGISTRY)


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
