"""One-shot script to download bundled libretro-database metadat DAT files.

Source: https://github.com/libretro/libretro-database

For every ROMulus system with a ``libretro_name`` set, fetch each
available metadata dimension's clrmamepro .dat file. Missing dimensions
for a given system (most systems don't have all seven) are silently
skipped — a 404 from GitHub raw means "no data for this combination".

Output layout::

    data/libretro-metadat/<dimension>/<libretro_name>.dat

The runtime client (:mod:`romulus.metadata.libretro_metadat`) walks the
same directory tree per-system to discover which dimensions are
available offline.

Re-run safely; the files are overwritten in place. Skips dimensions
listed in :data:`_DIMENSIONS` and only fetches DAT files (no JSON/RDB
variants).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from romulus.models.system import SYSTEM_REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "libretro-metadat"

# Metadata dimensions we surface in the UI today. ESRB / BBFC / ELSPA all
# map to ``rating``; we only pull ESRB to keep the bundle compact.
# ``franchise`` is plumbed through but not yet rendered — bundling it
# costs little and lets a future UI tweak surface it without re-downloading.
_DIMENSIONS: tuple[str, ...] = (
    "genre",
    "developer",
    "publisher",
    "releaseyear",
    "maxusers",
    "esrb",
    "franchise",
)

_BASE_URL = "https://github.com/libretro/libretro-database/raw/master/metadat"


def main() -> int:
    """Walk every registered system × dimension. Returns shell exit code."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    fetched = 0
    skipped = 0
    with httpx.Client(follow_redirects=True, timeout=60.0) as c:
        for sys_def in SYSTEM_REGISTRY:
            libretro_name = sys_def.libretro_name
            if not libretro_name:
                continue
            for dim in _DIMENSIONS:
                # GitHub raw URLs encode spaces with ``%20``; httpx handles
                # this for us if we just pass the literal name.
                url = f"{_BASE_URL}/{dim}/{libretro_name}.dat"
                try:
                    r = c.get(url)
                except httpx.HTTPError as exc:
                    print(
                        f"  {sys_def.id:18s} {dim:12s} ERR {exc}",
                        file=sys.stderr,
                    )
                    continue
                if r.status_code == 404:
                    skipped += 1
                    continue
                if r.status_code != 200:
                    print(
                        f"  {sys_def.id:18s} {dim:12s} status={r.status_code}",
                        file=sys.stderr,
                    )
                    continue
                dest_dir = OUT_DIR / dim
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f"{libretro_name}.dat"
                dest.write_bytes(r.content)
                total_bytes += len(r.content)
                fetched += 1
                print(
                    f"  {sys_def.id:18s} {dim:12s} -> "
                    f"{dest.name}  ({len(r.content) / 1024:.1f} KB)"
                )
    print(
        f"\nDownloaded {fetched} files "
        f"({total_bytes / 1024 / 1024:.2f} MB) to {OUT_DIR} "
        f"({skipped} 404s skipped)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
