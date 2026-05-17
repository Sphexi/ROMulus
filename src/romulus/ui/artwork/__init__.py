"""Artwork resolver — maps system ids + theme to bundled logo paths.

The PNG files live next to this package under ``systems/``. They were
extracted from the Dan Patrick "Console Logos (Professionally Redrawn +
Official Versions)" v2.1 Recommended set
(https://archive.org/details/console-logos-professionally-redrawn-plus-official-versions)
by ``scripts/extract_system_logos.py``.

Paths come from ``SystemDef.logo_dark`` / ``logo_light`` (loaded from
``systems/builtin.yaml``) and are relative to the directory holding this
module. The resolver returns ``None`` when no mapping exists or the file
is missing — callers must fall back to text in that case.
"""

from __future__ import annotations

from pathlib import Path

from romulus.models.system import SYSTEM_REGISTRY, SystemDef

#: Base directory for bundled artwork. ``Path(__file__).parent`` works in
#: both dev (the file sits under ``src/romulus/ui/artwork/``) and a frozen
#: PyInstaller exe (extracted to ``sys._MEIPASS/romulus/ui/artwork/``).
_ARTWORK_BASE: Path = Path(__file__).resolve().parent


def artwork_base_dir() -> Path:
    """Return the absolute path to the bundled artwork directory."""
    return _ARTWORK_BASE


def _theme_variant(theme_id: str | None) -> str:
    """Map a theme id from config to a logo variant key.

    "dark" -> dark logos (light-on-transparent), suitable for dark UIs.
    Everything else -> light logos (dark-on-transparent), suitable for
    light or system-default UIs. ``wbm_classic`` is a light theme too.
    """
    return "dark" if (theme_id or "") == "dark" else "light"


def _find_system_def(system_id: str) -> SystemDef | None:
    """Linear-scan the registry. ~80 entries; not worth a dict cache."""
    for entry in SYSTEM_REGISTRY:
        if entry.id == system_id:
            return entry
    return None


def resolve_system_logo(system_id: str, theme_id: str | None) -> Path | None:
    """Return the absolute path to the logo for *system_id* + *theme_id*.

    Returns ``None`` when:
      * the system id is unknown,
      * the SystemDef has no logo path for the selected variant, or
      * the file no longer exists on disk (e.g. unbundled by a packager).

    Callers should treat ``None`` as "no logo, render text instead".
    """
    sys_def = _find_system_def(system_id)
    if sys_def is None:
        return None
    variant = _theme_variant(theme_id)
    rel = sys_def.logo_dark if variant == "dark" else sys_def.logo_light
    if not rel:
        return None
    candidate = _ARTWORK_BASE / rel
    return candidate if candidate.is_file() else None
