"""One-shot script to download bundled GameDB data files.

Source: https://github.com/niemasd/GameDB

Each per-console repository publishes a ``<Console>.data.json`` file on
its latest GitHub release. This script downloads every file we have a
mapping for into ``data/gamedb/<system_id>.json``. Re-run safely; the
files are overwritten in place.

Only the consoles with both a GameDB repo *and* a ROMulus SystemDef are
mapped — see :data:`SYSTEM_TO_GAMEDB_REPO`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "gamedb"

# Map: ROMulus system_id -> GameDB repo suffix (after ``niemasd/GameDB-``).
# Built from the GitHub API listing of niemasd's repos against ROMulus's
# builtin.yaml system_ids. Systems with no GameDB repo (nds, virtualboy,
# mame, fbneo, neogeo, zxspectrum, amstradcpc, pcenginecd, n3ds, wiiu,
# ps3, psvita, j2me, palmos, plus the small/educational platforms) are
# absent intentionally — there is no upstream source.
SYSTEM_TO_GAMEDB_REPO: dict[str, str] = {
    # Nintendo
    "nes": "NES",
    "snes": "SNES",
    "n64": "N64",
    "n64dd": "64DD",
    "gamecube": "GC",
    "wii": "Wii",
    "gb": "GB",
    "gbc": "GBC",
    "gba": "GBA",
    "satellaview": "Satellaview",
    "sufami": "SuFamiTurbo",
    # Sega
    "megadrive": "Genesis",
    "mastersystem": "SMS",
    "gamegear": "GameGear",
    "saturn": "Saturn",
    "dreamcast": "Dreamcast",
    "sega32x": "32X",
    "sg1000": "SG1000",
    "segapico": "Pico",
    # Sony
    "psx": "PSX",
    "psp": "PSP",
    # Microsoft
    "xbox360": "XBOX360",
    # Atari
    "atari2600": "Atari2600",
    "atari5200": "Atari5200",
    "atari7800": "Atari7800",
    "jaguar": "Jaguar",
    "lynx": "Lynx",
    "atarist": "AtariST",
    # NEC
    "pcengine": "TurboGrafx16",
    "supergrafx": "SuperGrafx",
    # SNK
    "ngp": "NGP",
    "ngpc": "NGPC",
    # Bandai
    "wonderswan": "WonderSwan",
    "wonderswancolor": "WonderSwanColor",
    # Home computers
    "msx": "MSX",
    "amiga": "Amiga",
    "c64": "C64",
    # Niche
    "colecovision": "ColecoVision",
    "vectrex": "Vectrex",
    "supervision": "Supervision",
    "gamecom": "Gamecom",
    "casioloopy": "Loopy",
}


def main() -> int:
    """Download every mapped JSON file. Returns shell exit code."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    total_bytes = 0
    with httpx.Client(follow_redirects=True, timeout=60.0) as c:
        for sid, repo in sorted(SYSTEM_TO_GAMEDB_REPO.items()):
            url = (
                f"https://github.com/niemasd/GameDB-{repo}"
                f"/releases/latest/download/{repo}.data.json"
            )
            try:
                response = c.get(url)
            except httpx.HTTPError as exc:
                print(f"  {sid:18s} ERR network={exc}", file=sys.stderr)
                failures.append(sid)
                continue
            if response.status_code != 200:
                print(
                    f"  {sid:18s} ERR status={response.status_code}",
                    file=sys.stderr,
                )
                failures.append(sid)
                continue
            dest = OUT_DIR / f"{sid}.json"
            dest.write_bytes(response.content)
            total_bytes += len(response.content)
            print(
                f"  {sid:18s} {repo:18s} -> {dest.name}  "
                f"({len(response.content) / 1024:.1f} KB)"
            )
    print(
        f"\nDownloaded {len(SYSTEM_TO_GAMEDB_REPO) - len(failures)} / "
        f"{len(SYSTEM_TO_GAMEDB_REPO)} files "
        f"({total_bytes / 1024 / 1024:.2f} MB) to {OUT_DIR}"
    )
    if failures:
        print(f"Failures: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
