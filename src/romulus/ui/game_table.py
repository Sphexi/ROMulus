"""Game table — sortable, filterable ROM list with search."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtWidgets import (
    QHeaderView,
    QLineEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

COLUMNS = ("Name", "System", "Region", "Size", "Match")
# Cap rows loaded at once so very large libraries stay responsive.
DEFAULT_PAGE_SIZE = 5000


@dataclass(frozen=True)
class GameRow:
    """A single row rendered in the GameTable."""

    rom_id: int
    name: str
    system_id: str
    system_name: str
    region: str
    size_bytes: int
    match_confidence: str


def _format_size(size_bytes: int) -> str:
    """Human-readable byte size (KB / MB / GB)."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{int(size_bytes)} B"


def load_rom_rows(
    conn: sqlite3.Connection,
    system_id: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[GameRow]:
    """Pull ROM rows for the table (optionally filtered to one system)."""
    base = (
        "SELECT r.id, r.filename, r.system_id, "
        "COALESCE(s.short_name, s.display_name, r.system_id) AS sys_name, "
        "COALESCE(g.region, '') AS region, r.size_bytes, r.match_confidence "
        "FROM roms r "
        "LEFT JOIN systems s ON s.id = r.system_id "
        "LEFT JOIN games g ON g.id = r.game_id"
    )
    params: tuple = ()
    if system_id is not None:
        base += " WHERE r.system_id = ?"
        params = (system_id,)
    base += " ORDER BY r.filename LIMIT ?"
    params = params + (limit,)
    rows = conn.execute(base, params).fetchall()
    return [
        GameRow(
            rom_id=row[0],
            name=row[1],
            system_id=row[2] or "",
            system_name=row[3] or "",
            region=row[4] or "",
            size_bytes=int(row[5] or 0),
            match_confidence=row[6] or "unmatched",
        )
        for row in rows
    ]


class GameTableModel(QAbstractTableModel):
    """Plain QAbstractTableModel backed by a list of GameRow objects."""

    def __init__(self, rows: list[GameRow] | None = None) -> None:
        super().__init__()
        self._rows: list[GameRow] = list(rows or [])

    def set_rows(self, rows: list[GameRow]) -> None:
        """Replace the entire row set; emits modelReset to the view."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> GameRow:
        """Return the GameRow at a given row index."""
        return self._rows[row]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return row.name
            if col == 1:
                return row.system_name
            if col == 2:
                return row.region
            if col == 3:
                return _format_size(row.size_bytes)
            if col == 4:
                return row.match_confidence
            return None
        if role == Qt.ItemDataRole.UserRole:
            # Raw sort key — used by GameTableProxy for size sorting.
            if col == 3:
                return row.size_bytes
            return self.data(index, Qt.ItemDataRole.DisplayRole)
        return None


class GameTableProxy(QSortFilterProxyModel):
    """Proxy model — sorts by raw values (so 1 KB < 1 MB) and filters by name."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(0)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setDynamicSortFilter(True)


class GameTable(QWidget):
    """Search bar + sortable game table widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search games...")
        self.search.setClearButtonEnabled(True)

        self.model = GameTableModel()
        self.proxy = GameTableProxy(self)
        self.proxy.setSourceModel(self.model)

        self.view = QTableView(self)
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.view.horizontalHeader().setStretchLastSection(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search)
        layout.addWidget(self.view)

        self.search.textChanged.connect(self.proxy.setFilterFixedString)

    def set_rows(self, rows: list[GameRow]) -> None:
        """Hand the underlying model a fresh list of rows."""
        self.model.set_rows(rows)
