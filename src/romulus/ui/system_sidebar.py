"""System sidebar — lists systems with ROM counts and collections."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QMenu, QTreeView, QWidget

from romulus.db import get_config
from romulus.db import queries as q
from romulus.ui.artwork import resolve_system_logo

SYSTEM_ID_ROLE = Qt.ItemDataRole.UserRole + 1
NODE_KIND_ROLE = Qt.ItemDataRole.UserRole + 2

KIND_ALL = "all"
KIND_SYSTEM = "system"
KIND_COLLECTION = "collection"

# Logo height in pixels at sidebar zoom; width is whatever preserves the
# native aspect ratio (these logos are typically ~5:1 letterboxes). Bumped
# above the default Qt iconSize (16) because below ~20px the wordmarks
# turn to mush. The QTreeView iconSize cell is square at this height; the
# pixmap is rendered inside that, letterboxed.
_SIDEBAR_LOGO_HEIGHT = 22


def get_rom_counts_by_system(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Return [(system_id, display_name, rom_count)] for systems with >=1 ROM."""
    rows = conn.execute(
        """
        SELECT s.id, s.display_name, COUNT(r.id) AS n
        FROM systems s
        JOIN roms r ON r.system_id = s.id
        GROUP BY s.id, s.display_name
        ORDER BY s.display_name
        """
    ).fetchall()
    return [(row["id"], row["display_name"], row["n"]) for row in rows]


def get_total_rom_count(conn: sqlite3.Connection) -> int:
    """Return the count of all ROM rows in the database."""
    row = conn.execute("SELECT COUNT(*) AS n FROM roms").fetchone()
    return int(row["n"]) if row else 0


class SystemSidebar(QTreeView):
    """Tree view of systems and collections, emits selection changes."""

    system_selected = Signal(object)
    collection_selected = Signal(int)

    # Scoped action signals emitted from the right-click context menu.
    quick_scan_system_requested = Signal(str)   # system_id
    heavy_scan_system_requested = Signal(str)   # system_id
    enrich_system_requested = Signal(str)       # system_id
    find_covers_system_requested = Signal(str)  # system_id

    enrich_collection_requested = Signal(int)       # collection_id
    heavy_scan_collection_requested = Signal(int)   # collection_id
    find_covers_collection_requested = Signal(int)  # collection_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(["Library"])
        self.setModel(self._model)
        self.setHeaderHidden(True)
        self.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.selectionModel().currentChanged.connect(self._on_current_changed)
        # Give the icon column enough room for the wide logo PNGs. iconSize
        # only sets the *bounding* box; QStandardItem.setIcon() scales the
        # source pixmap into it preserving aspect ratio.
        self.setIconSize(QSize(_SIDEBAR_LOGO_HEIGHT * 3, _SIDEBAR_LOGO_HEIGHT))

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def populate(self, conn: sqlite3.Connection) -> None:
        """Rebuild the tree from the database."""
        self._model.removeRows(0, self._model.rowCount())
        theme = get_config(conn, "theme") or "system"

        total = get_total_rom_count(conn)
        all_item = QStandardItem(f"All ({total})")
        all_item.setData(KIND_ALL, NODE_KIND_ROLE)
        all_item.setData(None, SYSTEM_ID_ROLE)
        self._model.appendRow(all_item)

        systems_header = QStandardItem("Systems")
        systems_header.setSelectable(False)
        systems_header.setFlags(systems_header.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._model.appendRow(systems_header)
        for system_id, display_name, count in get_rom_counts_by_system(conn):
            item = QStandardItem(f"{display_name} ({count})")
            item.setData(KIND_SYSTEM, NODE_KIND_ROLE)
            item.setData(system_id, SYSTEM_ID_ROLE)
            icon = _logo_icon_for(system_id, theme)
            if icon is not None:
                item.setIcon(icon)
            systems_header.appendRow(item)
        self.expand(self._model.indexFromItem(systems_header))

        collections = q.get_collections(conn)
        if collections:
            collections_header = QStandardItem("Collections")
            collections_header.setSelectable(False)
            collections_header.setFlags(
                collections_header.flags() & ~Qt.ItemFlag.ItemIsSelectable
            )
            self._model.appendRow(collections_header)
            for row in collections:
                count = int(row["game_count"])
                item = QStandardItem(f"{row['name']} ({count})")
                item.setData(KIND_COLLECTION, NODE_KIND_ROLE)
                item.setData(int(row["id"]), SYSTEM_ID_ROLE)
                collections_header.appendRow(item)
            self.expand(self._model.indexFromItem(collections_header))

    def _on_current_changed(self, current, _previous) -> None:
        if not current.isValid():
            return
        kind = current.data(NODE_KIND_ROLE)
        payload = current.data(SYSTEM_ID_ROLE)
        if kind == KIND_ALL:
            self.system_selected.emit(None)
        elif kind == KIND_SYSTEM:
            self.system_selected.emit(payload)
        elif kind == KIND_COLLECTION and isinstance(payload, int):
            self.collection_selected.emit(payload)

    def _on_context_menu(self, point: object) -> None:
        """Build a context menu for system / collection rows."""
        index = self.indexAt(point)  # type: ignore[arg-type]
        if not index.isValid():
            return

        kind = index.data(NODE_KIND_ROLE)
        payload = index.data(SYSTEM_ID_ROLE)
        label = index.data(Qt.ItemDataRole.DisplayRole) or ""
        # Strip trailing " (N)" count from the display label for menu titles.
        display = label.rsplit(" (", 1)[0] if " (" in label else label

        if kind == KIND_SYSTEM and isinstance(payload, str):
            self._show_system_menu(point, payload, display)
        elif kind == KIND_COLLECTION and isinstance(payload, int):
            self._show_collection_menu(point, payload, display)
        # KIND_ALL and header rows get no menu.

    def _show_system_menu(
        self, point: object, system_id: str, display_name: str
    ) -> None:
        """Context menu for a system row."""
        menu = QMenu(self)

        quick = QAction(f"Quick Scan {display_name}", menu)
        quick.triggered.connect(
            lambda: self.quick_scan_system_requested.emit(system_id)
        )
        menu.addAction(quick)

        heavy = QAction(f"Heavy Scan {display_name}", menu)
        heavy.triggered.connect(
            lambda: self.heavy_scan_system_requested.emit(system_id)
        )
        menu.addAction(heavy)

        menu.addSeparator()

        enrich = QAction(f"Enrich {display_name}", menu)
        enrich.triggered.connect(
            lambda: self.enrich_system_requested.emit(system_id)
        )
        menu.addAction(enrich)

        covers = QAction(f"Find covers for {display_name}", menu)
        covers.triggered.connect(
            lambda: self.find_covers_system_requested.emit(system_id)
        )
        menu.addAction(covers)

        menu.exec(self.viewport().mapToGlobal(point))  # type: ignore[arg-type]

    def _show_collection_menu(
        self, point: object, collection_id: int, display_name: str
    ) -> None:
        """Context menu for a collection row."""
        menu = QMenu(self)

        enrich = QAction(f"Enrich games in {display_name}", menu)
        enrich.triggered.connect(
            lambda: self.enrich_collection_requested.emit(collection_id)
        )
        menu.addAction(enrich)

        heavy = QAction(f"Heavy Scan games in {display_name}", menu)
        heavy.triggered.connect(
            lambda: self.heavy_scan_collection_requested.emit(collection_id)
        )
        menu.addAction(heavy)

        covers = QAction(f"Find covers for games in {display_name}", menu)
        covers.triggered.connect(
            lambda: self.find_covers_collection_requested.emit(collection_id)
        )
        menu.addAction(covers)

        menu.exec(self.viewport().mapToGlobal(point))  # type: ignore[arg-type]


def _logo_icon_for(system_id: str, theme_id: str) -> QIcon | None:
    """Resolve a bundled platform logo into a sidebar-sized QIcon.

    Returns ``None`` when ``system_id`` has no logo for the current theme,
    or when the resolved PNG fails to decode. Callers must treat ``None``
    as "skip the icon, show text only".
    """
    path = resolve_system_logo(system_id, theme_id)
    if path is None:
        return None
    pix = QPixmap(str(path))
    if pix.isNull():
        return None
    scaled = pix.scaledToHeight(
        _SIDEBAR_LOGO_HEIGHT,
        Qt.TransformationMode.SmoothTransformation,
    )
    return QIcon(scaled)
