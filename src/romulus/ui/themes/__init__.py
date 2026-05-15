"""Theme system — QSS stylesheets bundled with Romulus.

``AVAILABLE_THEMES`` maps theme id -> display name.
``apply_theme`` applies the stylesheet app-wide.
``load_theme_qss`` returns the raw QSS string for a theme id.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

_THEMES_DIR = Path(__file__).resolve().parent

#: Maps theme-id -> human-readable display name shown in Settings.
AVAILABLE_THEMES: dict[str, str] = {
    "system": "System (default)",
    "dark": "Dark",
    "light": "Light",
    "wbm_classic": "WBM Classic",
}

# Theme ids that have a corresponding .qss file.  "system" uses no QSS.
_QSS_THEMES: frozenset[str] = frozenset({"dark", "light", "wbm_classic"})


def load_theme_qss(theme_id: str) -> str:
    """Return the QSS stylesheet text for *theme_id*.

    Returns an empty string for ``"system"`` (clears any applied stylesheet)
    and for any unknown id.  The QSS file is read on every call so a future
    hot-reload feature can swap files without restarting.
    """
    if theme_id not in _QSS_THEMES:
        return ""
    qss_path = _THEMES_DIR / f"{theme_id}.qss"
    try:
        return qss_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def apply_theme(app: QApplication, theme_id: str) -> None:
    """Apply *theme_id* to *app* by setting the global stylesheet.

    For ``"system"`` the stylesheet is cleared so Qt falls back to the OS
    native style.  Unknown ids are silently treated like ``"system"``.
    """
    app.setStyleSheet(load_theme_qss(theme_id))
