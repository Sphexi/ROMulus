"""System sidebar — lists systems with ROM counts and collections."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QTreeView, QWidget

from romulus.db import queries as q

SYSTEM_ID_ROLE = Qt.ItemDataRole.UserRole + 1
NODE_KIND_ROLE = Qt.ItemDataRole.UserRole + 2

KIND_ALL = "all"
KIND_SYSTEM = "system"
KIND_COLLECTION = "collection"


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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(["Library"])
        self.setModel(self._model)
        self.setHeaderHidden(True)
        self.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.selectionModel().currentChanged.connect(self._on_current_changed)

    def populate(self, conn: sqlite3.Connection) -> None:
        """Rebuild the tree from the database."""
        self._model.removeRows(0, self._model.rowCount())

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
