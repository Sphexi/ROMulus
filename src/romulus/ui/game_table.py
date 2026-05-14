"""Game table — sortable, filterable ROM list with search."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QTableView,
    QVBoxLayout,
    QWidget,
)

COLUMNS = ("Name", "System", "Region", "Size", "Match")
# Cap rows loaded at once so very large libraries stay responsive.
DEFAULT_PAGE_SIZE = 5000

REGION_FILTER_OPTIONS: tuple[str, ...] = (
    "All",
    "USA",
    "Europe",
    "Japan",
    "World",
    "Other",
)
MATCH_FILTER_OPTIONS: tuple[str, ...] = ("All", "Verified", "Unmatched")
# Regions that the "Other" bucket lumps together (anything not in this set).
_KNOWN_REGIONS: frozenset[str] = frozenset({"USA", "Europe", "Japan", "World"})


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
    game_id: int | None = None


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
    game_ids: list[int] | None = None,
) -> list[GameRow]:
    """Pull ROM rows for the table.

    `system_id` filters to a single platform. `game_ids` restricts the result
    to a specific set of game ids (used by collection views). Both can be
    combined.
    """
    base = (
        "SELECT r.id, r.filename, r.system_id, "
        "COALESCE(s.short_name, s.display_name, r.system_id) AS sys_name, "
        "COALESCE(g.region, '') AS region, r.size_bytes, r.match_confidence, "
        "r.game_id "
        "FROM roms r "
        "LEFT JOIN systems s ON s.id = r.system_id "
        "LEFT JOIN games g ON g.id = r.game_id"
    )
    clauses: list[str] = []
    params: list[object] = []
    if system_id is not None:
        clauses.append("r.system_id = ?")
        params.append(system_id)
    if game_ids is not None:
        if not game_ids:
            return []
        placeholders = ",".join("?" for _ in game_ids)
        clauses.append(f"r.game_id IN ({placeholders})")
        params.extend(game_ids)
    if clauses:
        base += " WHERE " + " AND ".join(clauses)
    base += " ORDER BY r.filename LIMIT ?"
    params.append(limit)
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
            game_id=int(row[7]) if row[7] is not None else None,
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
    """Proxy model — name search + region + match filters, numeric size sort."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(0)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setDynamicSortFilter(True)
        self._name_filter: str = ""
        self._region_filter: str = "All"
        self._match_filter: str = "All"

    def set_name_filter(self, text: str) -> None:
        """Substring (case-insensitive) match against the Name column."""
        self._name_filter = text or ""
        self.invalidate()

    def setFilterFixedString(self, text: str) -> None:
        """Override Qt's built-in name-filter so existing callers keep working."""
        self.set_name_filter(text)

    def set_region_filter(self, region: str) -> None:
        """Filter rows by region label; 'All' clears the filter."""
        self._region_filter = region or "All"
        self.invalidate()

    def set_match_filter(self, status: str) -> None:
        """Filter rows by match status (All / Verified / Unmatched)."""
        self._match_filter = status or "All"
        self.invalidate()

    def filterAcceptsRow(
        self,
        source_row: int,
        source_parent: QModelIndex,  # noqa: ARG002 - Qt API
    ) -> bool:
        model = self.sourceModel()
        if not isinstance(model, GameTableModel):
            return True
        row = model.row_at(source_row)
        if self._name_filter and self._name_filter.lower() not in row.name.lower():
            return False
        if self._region_filter != "All":
            row_region = row.region or ""
            if self._region_filter == "Other":
                if row_region in _KNOWN_REGIONS or row_region == "":
                    return False
            elif row_region != self._region_filter:
                return False
        if self._match_filter == "Verified":
            return row.match_confidence in {"dat_verified", "header"}
        if self._match_filter == "Unmatched":
            return row.match_confidence in {"unmatched", "fuzzy"}
        return True


class GameTable(QWidget):
    """Search bar + region/match filters + sortable game table widget."""

    game_selected = Signal(object)
    add_to_favorites_requested = Signal(int)
    add_to_collection_requested = Signal(int)
    new_collection_requested = Signal(str)
    remove_from_collection_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search games...")
        self.search.setClearButtonEnabled(True)

        self.region_filter = QComboBox(self)
        self.region_filter.addItems(REGION_FILTER_OPTIONS)
        self.match_filter = QComboBox(self)
        self.match_filter.addItems(MATCH_FILTER_OPTIONS)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.addWidget(self.search, 3)
        filter_row.addWidget(QLabel("Region:", self))
        filter_row.addWidget(self.region_filter, 1)
        filter_row.addWidget(QLabel("Match:", self))
        filter_row.addWidget(self.match_filter, 1)

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
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(filter_row)
        layout.addWidget(self.view)

        # Showing/hiding "Remove from Collection" depends on whether we're
        # currently filtered to a collection. MainWindow toggles this.
        self._collection_context: bool = False
        # Lookup map for context-menu collection submenu, set externally.
        self._available_collections: list[tuple[int, str]] = []

        self.search.textChanged.connect(self.proxy.set_name_filter)
        self.region_filter.currentTextChanged.connect(self.proxy.set_region_filter)
        self.match_filter.currentTextChanged.connect(self.proxy.set_match_filter)
        self.view.selectionModel().currentRowChanged.connect(
            self._on_current_row_changed
        )

    def set_rows(self, rows: list[GameRow]) -> None:
        """Hand the underlying model a fresh list of rows."""
        self.model.set_rows(rows)

    def set_collection_context(self, in_collection: bool) -> None:
        """Tell the table whether the current view is filtered to a collection."""
        self._collection_context = bool(in_collection)

    def set_available_collections(
        self, collections: list[tuple[int, str]]
    ) -> None:
        """Provide (id, name) pairs of user collections for the context menu."""
        self._available_collections = list(collections)

    def _selected_game_id(self) -> int | None:
        """Return the game_id for the currently-selected row, or None."""
        index = self.view.selectionModel().currentIndex()
        if not index.isValid():
            return None
        source_index = self.proxy.mapToSource(index)
        if not source_index.isValid():
            return None
        row = self.model.row_at(source_index.row())
        return row.game_id

    def _on_current_row_changed(
        self, current: QModelIndex, _previous: QModelIndex
    ) -> None:
        if not current.isValid():
            self.game_selected.emit(None)
            return
        source_index = self.proxy.mapToSource(current)
        if not source_index.isValid():
            self.game_selected.emit(None)
            return
        row = self.model.row_at(source_index.row())
        self.game_selected.emit(row.game_id)

    def _on_context_menu(self, point: object) -> None:
        game_id = self._selected_game_id()
        if game_id is None:
            return
        menu = QMenu(self.view)
        fav_action = QAction("Add to Favorites", menu)
        fav_action.triggered.connect(
            lambda: self.add_to_favorites_requested.emit(game_id)
        )
        menu.addAction(fav_action)

        add_menu = menu.addMenu("Add to Collection...")
        if self._available_collections:
            for cid, name in self._available_collections:
                action = QAction(name, add_menu)
                action.triggered.connect(
                    lambda _checked=False, _cid=cid: self.add_to_collection_requested.emit(
                        _cid
                    )
                )
                add_menu.addAction(action)
            add_menu.addSeparator()
        new_action = QAction("New Collection...", add_menu)
        new_action.triggered.connect(self._on_new_collection_request)
        add_menu.addAction(new_action)

        if self._collection_context:
            menu.addSeparator()
            remove_action = QAction("Remove from Collection", menu)
            remove_action.triggered.connect(
                lambda: self.remove_from_collection_requested.emit(game_id)
            )
            menu.addAction(remove_action)

        menu.exec(self.view.viewport().mapToGlobal(point))

    def _on_new_collection_request(self) -> None:
        name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
        if not ok:
            return
        trimmed = name.strip()
        if not trimmed:
            return
        self.new_collection_requested.emit(trimmed)
