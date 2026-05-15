"""Game detail panel — cover art, metadata, action buttons."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from romulus.db import queries as q
from romulus.db.queries import CONFIDENCE_RANK

COVER_WIDTH = 240
COVER_HEIGHT = 320
PLACEHOLDER_TEXT = "No cover art"

# Match confidence -> (label, CSS background color, foreground color).
_MATCH_BADGES: dict[str, tuple[str, str, str]] = {
    "dat_verified": ("DAT verified", "#2e7d32", "#ffffff"),
    "header": ("Header matched", "#b08900", "#ffffff"),
    "fuzzy": ("Fuzzy", "#6c757d", "#ffffff"),
    "unmatched": ("Unmatched", "#888888", "#ffffff"),
}


def _badge_text_for(confidence: str) -> tuple[str, str, str]:
    """Look up a badge label and colors for a match_confidence value."""
    return _MATCH_BADGES.get(confidence, _MATCH_BADGES["unmatched"])


# Recognized CSS color values for ``_match_badge_stylesheet``: hex (#rrggbb or
# #rgb) only. Anything else is rejected before interpolation into a Qt style
# sheet — even though today's _MATCH_BADGES values are hard-coded literals, a
# future "themable colours from config" feature must not be able to inject
# arbitrary CSS through the colour fields.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _match_badge_stylesheet(bg: str, fg: str) -> str:
    """Build the QLabel stylesheet for the match badge.

    Both colours are validated against :data:`_HEX_COLOR_RE` so a malformed
    constant (or a future user-supplied theme value) can never inject CSS
    syntax into the stylesheet. Invalid values fall back to neutral colours.
    """
    if not _HEX_COLOR_RE.match(bg):
        bg = "#888888"
    if not _HEX_COLOR_RE.match(fg):
        fg = "#ffffff"
    return (
        f"QLabel {{ background-color: {bg}; color: {fg}; "
        "border-radius: 6px; padding: 2px 6px; }"
    )


class DetailPanel(QWidget):
    """Right-side panel showing the currently-selected game's details."""

    favorite_toggled = Signal(int, bool)

    def __init__(
        self, conn: sqlite3.Connection, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._game_id: int | None = None
        self._favorites_id = q.ensure_favorites_collection(conn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Cover image area.
        self.cover_label = QLabel(self)
        self.cover_label.setFixedSize(COVER_WIDTH, COVER_HEIGHT)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.cover_label.setStyleSheet(
            "QLabel { background-color: #1e1e1e; color: #888; }"
        )
        cover_row = QHBoxLayout()
        cover_row.addStretch(1)
        cover_row.addWidget(self.cover_label)
        cover_row.addStretch(1)
        outer.addLayout(cover_row)

        # Title (bold, large).
        self.title_label = QLabel("Select a game", self)
        title_font = self.title_label.font()
        title_font.setPointSize(title_font.pointSize() + 4)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setWordWrap(True)
        outer.addWidget(self.title_label)

        # System line.
        self.system_label = QLabel("", self)
        self.system_label.setStyleSheet("QLabel { color: #888; }")
        outer.addWidget(self.system_label)

        # Match badge.
        self.match_badge = QLabel("", self)
        self.match_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.match_badge.setFixedHeight(22)
        self.match_badge.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        badge_row = QHBoxLayout()
        badge_row.addWidget(self.match_badge)
        badge_row.addStretch(1)
        outer.addLayout(badge_row)

        # Metadata fields.
        meta_layout = QVBoxLayout()
        meta_layout.setSpacing(2)
        self.region_label = QLabel("", self)
        self.revision_label = QLabel("", self)
        self.genre_label = QLabel("", self)
        self.developer_label = QLabel("", self)
        self.publisher_label = QLabel("", self)
        for w in (
            self.region_label,
            self.revision_label,
            self.genre_label,
            self.developer_label,
            self.publisher_label,
        ):
            w.setWordWrap(True)
            meta_layout.addWidget(w)
        outer.addLayout(meta_layout)

        # Description (scrollable read-only QTextEdit).
        self.description = QTextEdit(self)
        self.description.setReadOnly(True)
        self.description.setPlaceholderText("No description.")
        self.description.setMinimumHeight(80)
        outer.addWidget(self.description, 1)

        # ROM list (one row per linked ROM).
        self.rom_list_label = QLabel("ROM files:", self)
        self.rom_list_label.setStyleSheet("QLabel { color: #888; }")
        outer.addWidget(self.rom_list_label)
        self.rom_list = QTextEdit(self)
        self.rom_list.setReadOnly(True)
        self.rom_list.setMaximumHeight(80)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.rom_list)
        scroll.setMaximumHeight(90)
        outer.addWidget(scroll)

        # Action buttons row.
        actions = QHBoxLayout()
        self.favorite_button = QPushButton("☆ Favorite", self)
        self.favorite_button.setCheckable(True)
        self.favorite_button.clicked.connect(self._on_favorite_clicked)
        actions.addWidget(self.favorite_button)

        self.collection_button = QPushButton("Add to Collection...", self)
        self.collection_button.clicked.connect(self._show_collection_menu)
        actions.addWidget(self.collection_button)
        actions.addStretch(1)
        outer.addLayout(actions)

        self._render_empty()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_game(self, game_id: int | None) -> None:
        """Reload the panel for a new game id, or clear if None."""
        if game_id is None:
            self._game_id = None
            self._render_empty()
            return
        game = q.get_game_by_id(self._conn, game_id)
        if game is None:
            self._game_id = None
            self._render_empty()
            return
        self._game_id = int(game["id"])
        metadata = q.get_metadata(self._conn, self._game_id)
        covers = q.get_covers(self._conn, self._game_id)
        roms = q.get_roms_for_game(self._conn, self._game_id)
        self._render(game, metadata, covers, roms)

    @property
    def current_game_id(self) -> int | None:
        """Game currently shown, or None when blank."""
        return self._game_id

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _render_empty(self) -> None:
        """Reset to the "no selection" state."""
        self.cover_label.clear()
        self.cover_label.setText(PLACEHOLDER_TEXT)
        self.title_label.setText("Select a game")
        self.system_label.setText("")
        self.region_label.setText("")
        self.revision_label.setText("")
        self.genre_label.setText("")
        self.developer_label.setText("")
        self.publisher_label.setText("")
        self.description.clear()
        self.rom_list.clear()
        self.match_badge.setText("")
        self.match_badge.setStyleSheet("QLabel {}")
        self.favorite_button.setChecked(False)
        self.favorite_button.setEnabled(False)
        self.collection_button.setEnabled(False)

    def _render(
        self,
        game: sqlite3.Row,
        metadata: sqlite3.Row | None,
        covers: list[sqlite3.Row],
        roms: list[sqlite3.Row],
    ) -> None:
        """Populate every label / image from DB rows."""
        self.title_label.setText(str(game["title"]))
        self.system_label.setText(str(game["system_id"] or ""))
        self.region_label.setText(self._field("Region", game["region"]))
        self.revision_label.setText(self._field("Revision", game["revision"]))
        if metadata is not None:
            self.genre_label.setText(self._field("Genre", metadata["genre"]))
            self.developer_label.setText(
                self._field("Developer", metadata["developer"])
            )
            self.publisher_label.setText(
                self._field("Publisher", metadata["publisher"])
            )
            self.description.setPlainText(metadata["description"] or "")
        else:
            self.genre_label.setText("")
            self.developer_label.setText("")
            self.publisher_label.setText("")
            self.description.clear()

        # Match badge from the strongest match across linked ROMs.
        best_confidence = self._best_confidence(roms)
        label, bg, fg = _badge_text_for(best_confidence)
        self.match_badge.setText(f"  {label}  ")
        self.match_badge.setStyleSheet(_match_badge_stylesheet(bg, fg))

        # ROM list (filename + size on one line each).
        self.rom_list.setPlainText(self._format_rom_list(roms))

        # Cover image — use the first cover row that has a local file.
        self._render_cover(covers)

        # Favorites toggle reflects current membership.
        is_fav = q.is_game_in_collection(
            self._conn, self._favorites_id, int(game["id"])
        )
        self.favorite_button.blockSignals(True)
        self.favorite_button.setChecked(is_fav)
        self.favorite_button.setText(
            "★ Favorite" if is_fav else "☆ Favorite"
        )
        self.favorite_button.blockSignals(False)
        self.favorite_button.setEnabled(True)
        self.collection_button.setEnabled(True)

    def _render_cover(self, covers: list[sqlite3.Row]) -> None:
        """Load the first cover with a readable local file; else show placeholder."""
        for cover in covers:
            local = cover["local_path"]
            if not local:
                continue
            path = Path(str(local))
            if not path.exists():
                continue
            pix = QPixmap(str(path))
            if pix.isNull():
                continue
            scaled = pix.scaled(
                COVER_WIDTH,
                COVER_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.cover_label.setPixmap(scaled)
            self.cover_label.setText("")
            return
        self.cover_label.clear()
        self.cover_label.setText(PLACEHOLDER_TEXT)

    @staticmethod
    def _field(label: str, value: str | int | None) -> str:
        if value is None or value == "":
            return ""
        return f"{label}: {value}"

    @staticmethod
    def _best_confidence(roms: list[sqlite3.Row]) -> str:
        """Pick the highest-ranked match_confidence across ROMs for the game.

        Uses :data:`romulus.db.queries.CONFIDENCE_RANK` so the Python and SQL
        sides cannot drift (see queries.upsert_rom for the matching SQL CASE).
        """
        if not roms:
            return "unmatched"
        return max(
            (rom["match_confidence"] or "unmatched" for rom in roms),
            key=lambda c: CONFIDENCE_RANK.get(c, 0),
        )

    @staticmethod
    def _format_rom_list(roms: list[sqlite3.Row]) -> str:
        """Format the ROM list using full absolute paths with size annotation."""
        if not roms:
            return ""
        lines: list[str] = []
        for rom in roms:
            size = int(rom["size_bytes"] or 0)
            # Prefer the full path column; fall back to filename only.
            path = str(rom["path"] or rom["filename"] or "")
            confidence = str(rom["match_confidence"] or "unmatched")
            lines.append(f"{path}  ({size:,} B)  [{confidence}]")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_favorite_clicked(self) -> None:
        if self._game_id is None:
            return
        if self.favorite_button.isChecked():
            q.add_game_to_collection(
                self._conn, self._favorites_id, self._game_id
            )
            self.favorite_button.setText("★ Favorite")
            self.favorite_toggled.emit(self._game_id, True)
        else:
            q.remove_game_from_collection(
                self._conn, self._favorites_id, self._game_id
            )
            self.favorite_button.setText("☆ Favorite")
            self.favorite_toggled.emit(self._game_id, False)

    def _show_collection_menu(self) -> None:
        """Pop up an "Add to Collection..." menu with each user collection + New."""
        if self._game_id is None:
            return
        menu = QMenu(self)
        rows = q.get_collections(self._conn)
        for row in rows:
            if int(row["is_system"]):
                continue
            action = menu.addAction(str(row["name"]))
            action.setData(int(row["id"]))
        if rows:
            menu.addSeparator()
        new_action = menu.addAction("New Collection...")
        new_action.setData("__new__")
        chosen = menu.exec(
            self.collection_button.mapToGlobal(
                self.collection_button.rect().bottomLeft()
            )
        )
        if chosen is None:
            return
        payload = chosen.data()
        if payload == "__new__":
            name, ok = QInputDialog.getText(
                self, "New Collection", "Collection name:"
            )
            if not ok or not name.strip():
                return
            try:
                collection_id = q.create_collection(self._conn, name.strip())
            except sqlite3.IntegrityError:
                existing = q.get_collection_by_name(self._conn, name.strip())
                if existing is None:
                    return
                collection_id = int(existing["id"])
            q.add_game_to_collection(self._conn, collection_id, self._game_id)
        elif isinstance(payload, int):
            q.add_game_to_collection(self._conn, payload, self._game_id)
