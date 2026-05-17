"""One-shot script to extract console / handheld / computer logos.

Source: the Dan Patrick "v2.1 Recommended Versions (Normal)" archive from
https://archive.org/details/console-logos-professionally-redrawn-plus-official-versions
(zip placed in the repo root by the maintainer; not checked in).

For each ROMulus system id in ``SYSTEM_LOGO_MAP`` the script copies two
PNGs out of the zip, one from ``Dark - Color/`` and one from
``Light - Color/``, renamed to ``<system_id>-dark.png`` /
``<system_id>-light.png`` under ``src/romulus/ui/artwork/systems/``.

Re-run safely — it overwrites existing files. Systems with no entry in
``SYSTEM_LOGO_MAP`` fall back to the text label in the UI.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ZIP_NAME = (
    "v2.1_Recommended_Versions_(Normal)_(1_Per_Platform)_"
    "(Created_By_Dan_Patrick).zip"
)
OUT_DIR = REPO_ROOT / "src" / "romulus" / "ui" / "artwork" / "systems"

# Map: ROMulus system id -> relative path inside the zip, *below* the
# ``Dark - Color/`` (or ``Light - Color/``) directory. The script swaps the
# variant prefix in/out per pass.
#
# Values are either:
#   * a single str — same filename under both Dark and Light folders
#   * a (dark, light) tuple — Dan Patrick picked different "recommended"
#     files per variant (e.g. NEC PC Engine has ``-7-03`` in dark but a
#     plain name in light).
SYSTEM_LOGO_MAP: dict[str, str | tuple[str, str]] = {
    # Nintendo home consoles
    "nes": "Consoles/Nintendo Entertainment System.png",
    "snes": "Consoles/Super Nintendo Entertainment System.png",
    "n64": "Consoles/Nintendo 64.png",
    "n64dd": "Consoles/Nintendo 64DD.png",
    "gamecube": "Consoles/Nintendo GameCube.png",
    "wii": "Consoles/Nintendo Wii.png",
    "wiiu": "Consoles/Nintendo Wii U.png",
    "virtualboy": "Consoles/Nintendo Virtual Boy.png",
    "satellaview": "Consoles/Nintendo Satellaview.png",
    "sufami": "Consoles/Nintendo Sufami Turbo.png",
    # Nintendo handhelds
    "gb": "Handhelds/Nintendo Game Boy.png",
    "gbc": "Handhelds/Nintendo Game Boy Color.png",
    "gba": "Handhelds/Nintendo Game Boy Advance.png",
    "nds": "Handhelds/Nintendo DS.png",
    "dsiware": "Handhelds/Nintendo DSi Ware.png",
    "n3ds": "Handhelds/Nintendo 3DS.png",
    "pokemini": "Handhelds/Nintendo Pokémon Mini.png",
    "ereader": "Handhelds/Nintendo e-Reader.png",
    # Sega
    "megadrive": "Consoles/Sega Mega Drive.png",
    "mastersystem": "Consoles/Sega Master System.png",
    "gamegear": "Handhelds/Sega Game Gear.png",
    "saturn": "Consoles/Sega Saturn.png",
    "dreamcast": "Consoles/Sega Dreamcast.png",
    "sega32x": "Consoles/Sega 32X.png",
    "sg1000": "Consoles/Sega SG-1000.png",
    "segapico": "Computers/Sega Pico.png",
    # Sony
    "psx": "Consoles/Sony Playstation.png",
    "ps3": "Consoles/Sony Playstation 3.png",
    "psp": "Handhelds/Sony PSP.png",
    "psvita": "Handhelds/Sony PS Vita.png",
    # Microsoft
    "xbox360": "Consoles/Microsoft Xbox 360.png",
    # Atari
    "atari2600": "Consoles/Atari 2600.png",
    "atari5200": "Consoles/Atari 5200.png",
    "atari7800": "Consoles/Atari 7800.png",
    "jaguar": "Consoles/Atari Jaguar.png",
    "lynx": "Handhelds/Atari Lynx.png",
    "atarist": "Computers/Atari ST.png",
    # NEC
    "pcengine": (
        "Consoles/NEC PC Engine -7-03.png",
        "Consoles/NEC PC Engine.png",
    ),
    "pcenginecd": (
        "Consoles/NEC PC Engine CD -7-10.png",
        "Consoles/NEC PC Engine CD.png",
    ),
    "supergrafx": "Consoles/NEC PC Engine SuperGrafx.png",
    # SNK
    "neogeo": "Consoles/SNK Neo Geo.png",
    "ngp": "Handhelds/SNK Neo Geo Pocket.png",
    "ngpc": "Handhelds/SNK Neo Geo Pocket Color.png",
    # Bandai
    "wonderswan": "Handhelds/WonderSwan.png",
    "wonderswancolor": "Handhelds/Wonderswan Color.png",
    # Home computers
    "msx": "Computers/Microsoft MSX.png",
    "amiga": "Computers/Commodore Amiga.png",
    "c64": "Computers/Commodore 64.png",
    "c64plus4": "Computers/Commodore Plus 4.png",
    "vic20": "Computers/Commodore VIC-20.png",
    "zxspectrum": "Computers/Sinclair ZX Spectrum.png",
    "amstradcpc": "Computers/Amstrad CPC.png",
    # Arcade
    "mame": "Arcade/MAME.png",
    "fbneo": "Arcade/Final Burn Neo.png",
    # Classics & niche
    "colecovision": "Consoles/ColecoVision.png",
    "arcadia2001": "Consoles/Emerson Arcadia 2001.png",
    "adventurevision": "Handhelds/Entex Adventure Vision.png",
    "scv": "Consoles/Epoch Super Cassette Vision.png",
    "channelf": "Consoles/Fairchild Channel F.png",
    "superacan": "Consoles/Funtech Super Acan.png",
    "vectrex": "Consoles/GCE Vectrex.png",
    "gamemaster": "Handhelds/Hartung Game Master.png",
    "odyssey2": "Consoles/Magnavox Odyssey 2.png",
    "intellivision": "Consoles/Mattel Intellivision.png",
    "studio2": "Consoles/RCA Studio II.png",
    "gamecom": "Handhelds/Tiger Game.com.png",
    "creativision": "Consoles/VTech CreatiVision.png",
    "supervision": "Handhelds/Watara Supervision.png",
    "casioloopy": "Consoles/Casio Loopy.png",
    "pv1000": "Consoles/Casio PV-1000.png",
}

# Variant -> top-level folder name inside the zip's root directory.
VARIANTS = {"dark": "Dark - Color", "light": "Light - Color"}


def main() -> int:
    """Extract all logos. Returns shell-style exit code."""
    zip_path = REPO_ROOT / ZIP_NAME
    if not zip_path.is_file():
        print(f"ERROR: zip not found at {zip_path}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        # The zip wraps everything in a top-level folder matching the zip
        # name (without ``.zip``). Detect it dynamically so we don't hard-
        # code the verbose name into every lookup.
        roots = {name.split("/", 1)[0] for name in zf.namelist() if "/" in name}
        if len(roots) != 1:
            print(
                f"ERROR: expected one top-level dir in zip, got {roots}",
                file=sys.stderr,
            )
            return 1
        zip_root = roots.pop()

        missing: list[str] = []
        for system_id, rel in SYSTEM_LOGO_MAP.items():
            for variant, folder in VARIANTS.items():
                rel_for_variant = (
                    rel
                    if isinstance(rel, str)
                    else (rel[0] if variant == "dark" else rel[1])
                )
                src = f"{zip_root}/{folder}/{rel_for_variant}"
                dest = OUT_DIR / f"{system_id}-{variant}.png"
                try:
                    with zf.open(src) as fh:
                        dest.write_bytes(fh.read())
                except KeyError:
                    missing.append(src)

    if missing:
        print("Missing entries in zip:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 2
    print(f"Extracted {len(SYSTEM_LOGO_MAP) * 2} files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
