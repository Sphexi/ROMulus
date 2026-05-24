"""ROM detail panel — cover art, metadata, action buttons."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from romulus.db import get_config
from romulus.db import queries as q
from romulus.db.queries import (
    CONFIDENCE_RANK,  # noqa: F401  re-exported: test_db verifies single source
)
from romulus.models.system import SYSTEM_REGISTRY
from romulus.ui.artwork import resolve_system_logo

COVER_WIDTH = 240
COVER_HEIGHT = 320
PLACEHOLDER_TEXT = "No cover art"

# Pixel height of the platform logo shown beneath the cover. Width
# scales to whatever the source PNG's aspect ratio demands and is
# clamped to COVER_WIDTH at render time so the panel layout doesn't
# expand for wide wordmarks.
_SYSTEM_LOGO_HEIGHT = 48

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


_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def _format_bytes(n: int) -> str:
    """Format a byte count for human consumption.

    Decimal (1000-based) rather than binary — matches what most file
    explorers show.
    """
    size = float(n)
    for unit in _BYTE_UNITS:
        if size < 1000.0:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1000.0
    return f"{size:.1f} PB"


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
    """Right-side panel showing the currently-selected ROM's details."""

    favorite_toggled = Signal(int, bool)

    def __init__(
        self, conn: sqlite3.Connection, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._rom_id: int | None = None
        self._favorites_id = q.ensure_favorites_collection(conn)

        # Cover cycling state — all covers for the current ROM, current index.
        self._covers: list[sqlite3.Row] = []
        self._cover_index: int = 0

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

        # Cover navigation: ◀ | "2 of 5" | ▶ | ★ Make preferred
        nav_row = QHBoxLayout()
        nav_row.setSpacing(4)

        self.prev_button = QPushButton("◀", self)
        self.prev_button.setFixedWidth(32)
        self.prev_button.setToolTip("Previous cover")
        self.prev_button.clicked.connect(self._on_prev_cover)
        nav_row.addWidget(self.prev_button)

        self.cover_index_label = QLabel("", self)
        self.cover_index_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_index_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        nav_row.addWidget(self.cover_index_label)

        self.next_button = QPushButton("▶", self)
        self.next_button.setFixedWidth(32)
        self.next_button.setToolTip("Next cover")
        self.next_button.clicked.connect(self._on_next_cover)
        nav_row.addWidget(self.next_button)

        self.preferred_button = QPushButton("☆ Make preferred", self)
        self.preferred_button.setToolTip(
            "Set this cover as the default for this ROM"
        )
        self.preferred_button.clicked.connect(self._on_make_preferred)
        nav_row.addWidget(self.preferred_button)

        outer.addLayout(nav_row)

        # Title (bold, large).
        self.title_label = QLabel("Select a ROM", self)
        title_font = self.title_label.font()
        title_font.setPointSize(title_font.pointSize() + 4)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setWordWrap(True)
        outer.addWidget(self.title_label)

        # System indicator — a platform logo when one is bundled for the
        # current theme, falling back to the system's display name in
        # plain text. One QLabel handles both paths; only one of
        # ``setPixmap`` / ``setText`` is set at a time.
        self.system_label = QLabel("", self)
        self.system_label.setMinimumHeight(_SYSTEM_LOGO_HEIGHT)
        self.system_label.setMaximumHeight(_SYSTEM_LOGO_HEIGHT)
        self.system_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Subtle grey when this is being used as a text fallback. The
        # stylesheet has no effect on the pixmap path so it's safe to
        # leave applied unconditionally.
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

        # Metadata grid — one QLabel per field, paired against a value
        # label in a two-column QFormLayout. Rows whose value is empty
        # are hidden in :meth:`_set_field` so the grid stays tight even
        # for un-enriched ROMs (which is the common case).
        self._meta_grid = QFormLayout()
        self._meta_grid.setSpacing(2)
        self._meta_grid.setContentsMargins(0, 0, 0, 0)
        self._meta_grid.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._meta_grid.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        # (key, display label) — order is the on-screen render order.
        # Keys map to: rom.region/revision, computed rom size + sha1,
        # path (new — the full path to this specific ROM file),
        # and metadata.{genre,developer,publisher,release_date,players,rating}.
        self._meta_field_specs: tuple[tuple[str, str], ...] = (
            ("region", "Region"),
            ("revision", "Revision"),
            ("size", "ROM size"),
            ("sha1", "SHA-1"),
            ("dat_name", "DAT name"),
            ("path", "Path"),
            ("genre", "Genre"),
            ("developer", "Developer"),
            ("publisher", "Publisher"),
            ("release_date", "Released"),
            ("players", "Players"),
            ("rating", "Rating"),
        )
        self._meta_value_labels: dict[str, QLabel] = {}
        self._meta_key_labels: dict[str, QLabel] = {}
        for key, display in self._meta_field_specs:
            value = QLabel("", self)
            value.setWordWrap(True)
            value.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label = QLabel(f"{display}:", self)
            label.setStyleSheet("QLabel { color: #888; }")
            self._meta_grid.addRow(label, value)
            self._meta_value_labels[key] = value
            self._meta_key_labels[key] = label
        outer.addLayout(self._meta_grid)

        # Description — a plain wrapped QLabel that hides entirely when
        # no description text is available, so the panel doesn't reserve
        # vertical space for an empty box (which it usually was). Selectable
        # so the user can copy quotes out.
        self.description = QLabel("", self)
        self.description.setWordWrap(True)
        self.description.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.description.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.description.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.description.hide()
        outer.addWidget(self.description, 1)

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

    def update_rom(self, rom_id: int | None) -> None:
        """Reload the panel for a new ROM id, or clear if None."""
        if rom_id is None:
            self._rom_id = None
            self._render_empty()
            return
        rom = q.get_rom_by_id(self._conn, rom_id)
        if rom is None:
            self._rom_id = None
            self._render_empty()
            return
        self._rom_id = int(rom["id"])
        metadata = q.get_metadata(self._conn, self._rom_id)
        # Cycle through Named_Boxarts only: the panel displays a single image
        # slot and ``is_preferred`` is scoped per (rom_id, cover_type), so
        # mixing types into one cycle produces multiple "preferred" covers
        # (one per type) and the Make-preferred button stays disabled on most
        # of them. Snaps/Titles are still in the DB for the exporter to ship
        # to gamelist.xml; the UI just filters this view to boxarts.
        all_covers = q.get_covers(self._conn, self._rom_id)
        covers = [c for c in all_covers if c["cover_type"] == "Named_Boxarts"]
        # If a ROM somehow has no boxart but has other types, fall back so
        # the user still sees *something*.
        if not covers and all_covers:
            covers = list(all_covers)
        self._covers = covers
        self._cover_index = 0
        self._render(rom, metadata, covers)

    def update_game(self, rom_id: int | None) -> None:
        """Deprecated alias for :meth:`update_rom` kept for compatibility."""
        self.update_rom(rom_id)

    @property
    def current_rom_id(self) -> int | None:
        """ROM currently shown, or None when blank."""
        return self._rom_id

    @property
    def current_game_id(self) -> int | None:
        """Deprecated alias for :attr:`current_rom_id` kept for compatibility."""
        return self._rom_id

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _render_empty(self) -> None:
        """Reset to the "no selection" state."""
        self._covers = []
        self._cover_index = 0
        self.cover_label.clear()
        self.cover_label.setText(PLACEHOLDER_TEXT)
        self.cover_label.setToolTip("")
        self.title_label.setText("Select a ROM")
        self.system_label.clear()
        self.system_label.setText("")
        for key in self._meta_value_labels:
            self._set_field(key, None)
        self.description.clear()
        self.description.hide()
        self.match_badge.setText("")
        self.match_badge.setStyleSheet("QLabel {}")
        self.favorite_button.setChecked(False)
        self.favorite_button.setEnabled(False)
        self.collection_button.setEnabled(False)
        self._update_cover_nav()

    def _render(
        self,
        rom: sqlite3.Row,
        metadata: sqlite3.Row | None,
        covers: list[sqlite3.Row],
    ) -> None:
        """Populate every label / image from DB rows.

        With the 1:1 rom-keyed model every field is read directly from the
        ``roms`` row — no LIMIT 1 ambiguity across multiple ROM files.
        """
        # Title: prefer the canonical_name from DAT matching when available;
        # fall back to the parsed filename title, then the bare filename.
        title = (
            rom["canonical_name"]
            or rom["title"]
            or rom["filename"]
            or ""
        )
        self.title_label.setText(str(title))
        self._render_system_indicator(str(rom["system_id"] or ""))

        self._set_field("region", rom["region"])
        self._set_field("revision", rom["revision"])

        # ROM size — read directly from the roms row.
        size_bytes = int(rom["size_bytes"] or 0)
        self._set_field("size", _format_bytes(size_bytes) if size_bytes > 0 else None)

        # SHA-1 — look up from hashes table for this specific rom_id.
        sha1_row = self._conn.execute(
            "SELECT sha1 FROM hashes WHERE rom_id = ? AND sha1 IS NOT NULL LIMIT 1",
            (self._rom_id,),
        ).fetchone()
        sha1_raw = sha1_row[0] if sha1_row else None
        self._set_field("sha1", self._format_sha1(sha1_raw))

        # DAT name — unambiguous: this rom's dat_match if dat_verified,
        # else canonical_name, else None.
        confidence = str(rom["match_confidence"] or "")
        if confidence == "dat_verified" and rom["dat_match"]:
            dat_name: str | None = str(rom["dat_match"])
        elif rom["canonical_name"]:
            dat_name = str(rom["canonical_name"])
        else:
            dat_name = None
        self._set_field("dat_name", dat_name)

        # Path — full forward-slash path to this ROM file on disk.
        rom_path = str(rom["path"] or "")
        self._set_field("path", rom_path if rom_path else None)

        if metadata is not None:
            self._set_field("genre", metadata["genre"])
            self._set_field("developer", metadata["developer"])
            self._set_field("publisher", metadata["publisher"])
            # Prefer the fuller release_date when present; fall back to the
            # year-only value GameDB sometimes provides. Either fills the
            # same "Released" grid row.
            self._set_field(
                "release_date",
                metadata["release_date"] or metadata["release_year"],
            )
            self._set_field("players", metadata["players"])
            self._set_field("rating", metadata["rating"])
            self._set_description(metadata["description"])
        else:
            for key in ("genre", "developer", "publisher", "release_date",
                        "players", "rating"):
                self._set_field(key, None)
            self._set_description(None)

        # Match badge — reads directly from this rom's match_confidence.
        label, bg, fg = _badge_text_for(confidence)
        self.match_badge.setText(f"  {label}  ")
        self.match_badge.setStyleSheet(_match_badge_stylesheet(bg, fg))

        # Cover viewer — display current index and update nav controls.
        self._render_cover_at_index()
        self._update_cover_nav()

        # Favorites toggle reflects current membership.
        is_fav = q.is_rom_in_collection(
            self._conn, self._favorites_id, int(self._rom_id)  # type: ignore[arg-type]
        )
        self.favorite_button.blockSignals(True)
        self.favorite_button.setChecked(is_fav)
        self.favorite_button.setText(
            "★ Favorite" if is_fav else "☆ Favorite"
        )
        self.favorite_button.blockSignals(False)
        self.favorite_button.setEnabled(True)
        self.collection_button.setEnabled(True)

    def _render_system_indicator(self, system_id: str) -> None:
        """Paint the platform logo for *system_id*; fall back to display name.

        Reads the current theme from config on every refresh so a theme
        switch picks up the right variant without needing a panel-wide
        rebuild. Empty system ids leave the label blank.
        """
        if not system_id:
            self.system_label.clear()
            self.system_label.setText("")
            self.system_label.setToolTip("")
            return
        theme = get_config(self._conn, "theme") or "system"
        path = resolve_system_logo(system_id, theme)
        if path is not None:
            pix = QPixmap(str(path))
            if not pix.isNull():
                scaled = pix.scaled(
                    COVER_WIDTH,
                    _SYSTEM_LOGO_HEIGHT,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.system_label.setPixmap(scaled)
                self.system_label.setText("")
                self.system_label.setToolTip(self._display_name_for(system_id))
                return
        # No logo (or pixmap failed to load) — fall back to display name.
        self.system_label.clear()
        self.system_label.setText(self._display_name_for(system_id))
        self.system_label.setToolTip("")

    @staticmethod
    def _display_name_for(system_id: str) -> str:
        """Resolve a system id to its human-readable display name.

        Falls back to the raw id when the registry doesn't know it — same
        behaviour as the pre-logo path showed for unknown systems.
        """
        for entry in SYSTEM_REGISTRY:
            if entry.id == system_id:
                return entry.display_name
        return system_id

    def _render_cover_at_index(self) -> None:
        """Display the cover at ``self._cover_index``; show placeholder if none."""
        covers = self._covers
        if not covers:
            self.cover_label.clear()
            self.cover_label.setText(PLACEHOLDER_TEXT)
            self.cover_label.setToolTip("")
            return

        # Clamp index defensively.
        idx = max(0, min(self._cover_index, len(covers) - 1))
        cover = covers[idx]
        local = cover["local_path"]
        if local:
            path = Path(str(local))
            if path.exists():
                pix = QPixmap(str(path))
                if not pix.isNull():
                    scaled = pix.scaled(
                        COVER_WIDTH,
                        COVER_HEIGHT,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self.cover_label.setPixmap(scaled)
                    self.cover_label.setText("")
                    self.cover_label.setToolTip(str(path))
                    return
        # Fallback: no readable file for this cover row.
        self.cover_label.clear()
        self.cover_label.setText(PLACEHOLDER_TEXT)
        self.cover_label.setToolTip("")

    def _update_cover_nav(self) -> None:
        """Refresh the navigation bar (index label + button states).

        The Make-preferred button reflects the state of the *currently
        displayed* cover: filled star + disabled when this cover is already
        preferred, hollow star + enabled when clicking would promote it.
        Without this feedback the button felt no-op'y because nothing
        visibly changed on click.
        """
        n = len(self._covers)
        has_multiple = n > 1
        self.prev_button.setEnabled(has_multiple)
        self.next_button.setEnabled(has_multiple)

        if n == 0:
            self.cover_index_label.setText("")
            self.preferred_button.setEnabled(False)
            self.preferred_button.setText("☆ Make preferred")
            return

        idx = max(0, min(self._cover_index, n - 1))
        current = self._covers[idx]
        # ``is_preferred`` is guaranteed by the schema migration in
        # ``db/schema.py:_migrate_covers_add_is_preferred`` which runs at
        # startup; an IndexError on a malformed Row would be a real bug.
        try:
            is_pref = bool(current["is_preferred"])
        except (IndexError, KeyError):
            is_pref = False

        marker = "★ " if is_pref else ""
        self.cover_index_label.setText(f"{marker}{idx + 1} of {n}")

        if is_pref:
            self.preferred_button.setText("★ Preferred")
            self.preferred_button.setEnabled(False)
            self.preferred_button.setToolTip("This cover is already the default for this ROM.")
        else:
            self.preferred_button.setText("☆ Make preferred")
            self.preferred_button.setEnabled(True)
            self.preferred_button.setToolTip(
                "Set this cover as the default for this ROM"
            )

    # ------------------------------------------------------------------
    # Cover navigation slots
    # ------------------------------------------------------------------

    def _on_prev_cover(self) -> None:
        """Step backward through covers (wraps around)."""
        n = len(self._covers)
        if n < 2:
            return
        self._cover_index = (self._cover_index - 1) % n
        self._render_cover_at_index()
        self._update_cover_nav()

    def _on_next_cover(self) -> None:
        """Step forward through covers (wraps around)."""
        n = len(self._covers)
        if n < 2:
            return
        self._cover_index = (self._cover_index + 1) % n
        self._render_cover_at_index()
        self._update_cover_nav()

    def _on_make_preferred(self) -> None:
        """Mark the currently displayed cover as preferred, then reload."""
        if not self._covers or self._rom_id is None:
            return
        idx = max(0, min(self._cover_index, len(self._covers) - 1))
        cover_id = int(self._covers[idx]["id"])
        q.set_preferred_cover(self._conn, cover_id)
        # Reload + re-filter to the same cover_type the cycle is restricted
        # to. update_rom()'s filter mirrors this logic; keep them in sync.
        all_covers = q.get_covers(self._conn, self._rom_id)
        self._covers = [
            c for c in all_covers if c["cover_type"] == "Named_Boxarts"
        ] or list(all_covers)
        self._cover_index = 0
        self._render_cover_at_index()
        self._update_cover_nav()

    # ------------------------------------------------------------------
    # Metadata grid helpers
    # ------------------------------------------------------------------

    def _set_field(self, key: str, value: object | None) -> None:
        """Populate or hide the grid row for ``key``.

        Empty / None / zero-length values hide *both* the key label and
        the value cell so the surrounding rows close up tight. Non-empty
        values are stringified with ``str()`` — callers that need
        special formatting (size, sha1) hand in already-formatted text.
        """
        if value is None or value == "":
            self._meta_key_labels[key].hide()
            self._meta_value_labels[key].setText("")
            self._meta_value_labels[key].hide()
            return
        self._meta_value_labels[key].setText(str(value))
        self._meta_key_labels[key].show()
        self._meta_value_labels[key].show()

    def _set_description(self, value: str | None) -> None:
        """Show the description label when non-empty, hide it otherwise."""
        if not value:
            self.description.clear()
            self.description.hide()
            return
        self.description.setText(value)
        self.description.show()

    @staticmethod
    def _format_sha1(sha1: str | None) -> str | None:
        """Truncate a 40-char SHA-1 into a 12-char display form."""
        if not sha1:
            return None
        value = str(sha1)
        if len(value) > 12:
            return f"{value[:8]}…{value[-4:]}"
        return value

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_favorite_clicked(self) -> None:
        if self._rom_id is None:
            return
        if self.favorite_button.isChecked():
            q.add_rom_to_collection(
                self._conn, self._favorites_id, self._rom_id
            )
            self.favorite_button.setText("★ Favorite")
            self.favorite_toggled.emit(self._rom_id, True)
        else:
            q.remove_rom_from_collection(
                self._conn, self._favorites_id, self._rom_id
            )
            self.favorite_button.setText("☆ Favorite")
            self.favorite_toggled.emit(self._rom_id, False)

    def _show_collection_menu(self) -> None:
        """Pop up an "Add to Collection..." menu with each user collection + New."""
        if self._rom_id is None:
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
            q.add_rom_to_collection(self._conn, collection_id, self._rom_id)
        elif isinstance(payload, int):
            q.add_rom_to_collection(self._conn, payload, self._rom_id)
