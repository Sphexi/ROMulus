"""Game table — sortable, filterable ROM list with search."""

from __future__ import annotations

import logging
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

from romulus.db import queries as q

logger = logging.getLogger(__name__)

# Column indices — keep in sync with COLUMNS tuple.
_COL_NAME = 0
_COL_SYSTEM = 1
_COL_REGION = 2
_COL_SIZE = 3
_COL_MATCH = 4
_COL_PATH = 5

COLUMNS = ("Name", "System", "Region", "Size", "Match", "Path")
# Cap rows loaded at once so very large libraries stay responsive.
DEFAULT_PAGE_SIZE = 5000

REGION_FILTER_OPTIONS: tuple[str, ...] = (
    "All",
    "USA",
    "Europe",
    "Japan",
    "World",
    "Other",
    "None (no region)",
)
MATCH_FILTER_OPTIONS: tuple[str, ...] = (
    "All",
    "Verified",
    "Fuzzy",
    "Unmatched",
)
ENRICHMENT_FILTER_OPTIONS: tuple[str, ...] = (
    "All",
    "Has cover",
    "Has metadata",
    "Has both",
    "Has neither",
)
# Regions that the "Other" bucket lumps together (anything not in this set).
_KNOWN_REGIONS: frozenset[str] = frozenset({"USA", "Europe", "Japan", "World"})
# Hoisted out of filterAcceptsRow so the proxy doesn't allocate a fresh set
# every time Qt invokes the filter (once per row per filter change).
_VERIFIED_CONFIDENCES: frozenset[str] = frozenset({"dat_verified", "header"})


@dataclass(frozen=True)
class RomRow:
    """A single row rendered in the ROM table — one row per ROM file."""

    rom_id: int
    name: str
    system_id: str
    system_name: str
    region: str
    size_bytes: int
    match_confidence: str
    rom_path: str = ""
    has_cover: bool = False
    has_metadata: bool = False


# Backwards-compatible alias — existing test imports of ``GameRow`` continue
# to work without change while the codebase migrates to ``RomRow``.
GameRow = RomRow


def _format_size(size_bytes: int) -> str:
    """Human-readable byte size (KB / MB / GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def load_rom_rows(
    conn: sqlite3.Connection,
    system_id: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    rom_ids: list[int] | None = None,
    game_ids: list[int] | None = None,
) -> list[RomRow]:
    """Pull ROM rows for the table, including enrichment status and path.

    `system_id` filters to a single platform. `rom_ids` restricts the result
    to a specific set of rom ids (used by collection views). `game_ids` is a
    deprecated alias for `rom_ids` kept for call-site compatibility.
    """
    effective_ids = rom_ids if rom_ids is not None else game_ids
    rows = q.get_roms_with_enrichment_status(
        conn, system_id=system_id, rom_ids=effective_ids, limit=limit
    )
    return [
        RomRow(
            rom_id=int(row["rom_id"]),
            name=str(row["name"] or ""),
            system_id=str(row["system_id"] or ""),
            system_name=str(row["system_name"] or ""),
            region=str(row["region"] or ""),
            size_bytes=int(row["size_bytes"] or 0),
            match_confidence=str(row["match_confidence"] or "unmatched"),
            rom_path=str(row["rom_path"] or ""),
            has_cover=bool(int(row["has_cover"] or 0)),
            has_metadata=bool(int(row["has_metadata"] or 0)),
        )
        for row in rows
    ]


class GameTableModel(QAbstractTableModel):
    """Plain QAbstractTableModel backed by a list of RomRow objects."""

    def __init__(self, rows: list[RomRow] | None = None) -> None:
        super().__init__()
        self._rows: list[RomRow] = list(rows or [])

    def set_rows(self, rows: list[RomRow]) -> None:
        """Replace the entire row set; emits modelReset to the view."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> RomRow:
        """Return the RomRow at a given row index."""
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
            if col == _COL_NAME:
                return row.name
            if col == _COL_SYSTEM:
                return row.system_name
            if col == _COL_REGION:
                return row.region
            if col == _COL_SIZE:
                return _format_size(row.size_bytes)
            if col == _COL_MATCH:
                return row.match_confidence
            if col == _COL_PATH:
                return row.rom_path
            return None
        if role == Qt.ItemDataRole.UserRole:
            # Raw sort key — used by GameTableProxy for size sorting.
            if col == _COL_SIZE:
                return row.size_bytes
            return self.data(index, Qt.ItemDataRole.DisplayRole)
        if role == Qt.ItemDataRole.ToolTipRole and col == _COL_PATH:
            return row.rom_path
        return None


class GameTableProxy(QSortFilterProxyModel):
    """Proxy model — name search + region + match + enrichment filters."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(0)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setDynamicSortFilter(True)
        self._name_filter: str = ""
        self._region_filter: str = "All"
        self._match_filter: str = "All"
        self._enrichment_filter: str = "All"

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
        """Filter rows by match status (All / Verified / Fuzzy / Unmatched)."""
        self._match_filter = status or "All"
        self.invalidate()

    def set_enrichment_filter(self, value: str) -> None:
        """Filter rows by enrichment status (All / Has cover / Has metadata / …)."""
        self._enrichment_filter = value or "All"
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

        # Name search
        if self._name_filter and self._name_filter.lower() not in row.name.lower():
            return False

        # Region filter
        if self._region_filter != "All":
            row_region = row.region or ""
            if self._region_filter == "None (no region)":
                if row_region != "":
                    return False
            elif self._region_filter == "Other":
                if row_region in _KNOWN_REGIONS or row_region == "":
                    return False
            elif row_region != self._region_filter:
                return False

        # Match filter — Fuzzy is now separate from Unmatched
        if self._match_filter == "Verified":
            if row.match_confidence not in _VERIFIED_CONFIDENCES:
                return False
        elif self._match_filter == "Fuzzy":
            if row.match_confidence != "fuzzy":
                return False
        elif self._match_filter == "Unmatched" and row.match_confidence != "unmatched":
            return False

        # Enrichment filter
        if self._enrichment_filter == "Has cover":
            if not row.has_cover:
                return False
        elif self._enrichment_filter == "Has metadata":
            if not row.has_metadata:
                return False
        elif self._enrichment_filter == "Has both":
            if not (row.has_cover and row.has_metadata):
                return False
        elif self._enrichment_filter == "Has neither" and (row.has_cover or row.has_metadata):
            return False

        return True


class GameTable(QWidget):
    """Search bar + region/match/enrichment filters + sortable ROM table widget."""

    rom_selected = Signal(object)
    add_to_favorites_requested = Signal(int)
    add_to_collection_requested = Signal(int)
    new_collection_requested = Signal(str)
    remove_from_collection_requested = Signal(int)
    # Scoped action signals — carry the rom_id of the selected row.
    enrich_rom_requested = Signal(int)
    heavy_scan_rom_requested = Signal(int)
    find_local_covers_rom_requested = Signal(int)
    # File-system actions — carry the rom_id (the literal table row).
    # Reveal / delete must operate on the specific row the user clicked.
    reveal_rom_requested = Signal(int)
    delete_rom_requested = Signal(int)

    # Deprecated signal aliases kept for call-site compatibility.
    game_selected = rom_selected

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search games...")
        self.search.setClearButtonEnabled(True)

        self.region_filter = QComboBox(self)
        self.region_filter.addItems(REGION_FILTER_OPTIONS)
        self.match_filter = QComboBox(self)
        self.match_filter.addItems(MATCH_FILTER_OPTIONS)
        self.enrichment_filter = QComboBox(self)
        self.enrichment_filter.addItems(ENRICHMENT_FILTER_OPTIONS)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.addWidget(self.search, 3)
        filter_row.addWidget(QLabel("Region:", self))
        filter_row.addWidget(self.region_filter, 1)
        filter_row.addWidget(QLabel("Match:", self))
        filter_row.addWidget(self.match_filter, 1)
        filter_row.addWidget(QLabel("Enrichment:", self))
        filter_row.addWidget(self.enrichment_filter, 1)

        self.model = GameTableModel()
        self.proxy = GameTableProxy(self)
        self.proxy.setSourceModel(self.model)

        self.view = QTableView(self)
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        # Default sort: Name ascending (A→Z). Without this Qt leaves the
        # sort indicator off and rows fall in whatever order the underlying
        # query returned, which surfaced as Z-first on the user's library.
        self.view.sortByColumn(_COL_NAME, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        # All columns are Interactive so the user can drag-resize every one
        # including Name. Pure Qt Stretch mode disables manual resize on the
        # stretched column, which the user kept noticing; pure Interactive
        # leaves a gap when columns don't fill the viewport. We simulate the
        # "fill remaining space" behavior ourselves: until the user manually
        # drags the Name column, ``_adjust_name_column`` keeps it sized to
        # the leftover viewport width. After a manual drag the user's choice
        # sticks forever.
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(80)
        self.view.setColumnWidth(_COL_NAME, 360)  # initial; auto-grown below
        self.view.setColumnWidth(_COL_SYSTEM, 110)
        self.view.setColumnWidth(_COL_REGION, 90)
        self.view.setColumnWidth(_COL_SIZE, 80)
        self.view.setColumnWidth(_COL_MATCH, 90)
        self.view.setColumnWidth(_COL_PATH, 280)
        # Track whether the user has explicitly resized Name. While False,
        # Name auto-grows to fill leftover space; once True, we never touch
        # it again.
        self._name_user_resized: bool = False
        self._adjusting_name_width: bool = False
        header.sectionResized.connect(self._on_section_resized)
        # Disable in-cell ellipsis on the Path column so wide cells render in
        # full when the column is dragged wider (or auto-fitted below).
        self.view.setTextElideMode(Qt.TextElideMode.ElideNone)
        # Horizontal scrollbar appears when the Path column outgrows the view.
        self.view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
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
        self.enrichment_filter.currentTextChanged.connect(
            self.proxy.set_enrichment_filter
        )
        self.view.selectionModel().currentRowChanged.connect(
            self._on_current_row_changed
        )

    def set_rows(self, rows: list[RomRow]) -> None:
        """Hand the underlying model a fresh list of rows.

        Auto-fits the Path column to its widest entry so full Windows paths
        render without ellipsis, then re-expands Name to absorb leftover
        viewport space (unless the user has manually resized Name).
        """
        self.model.set_rows(rows)
        self.view.resizeColumnToContents(_COL_PATH)
        self._adjust_name_column()

    def resizeEvent(self, event: object) -> None:  # type: ignore[override]
        """Re-fill Name to leftover space whenever the widget resizes.

        Stays a no-op once the user has manually dragged Name to a chosen
        width — see ``_on_section_resized``.
        """
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._adjust_name_column()

    def _on_section_resized(
        self, logical_index: int, _old_size: int, _new_size: int
    ) -> None:
        """Detect user-initiated drag on the Name column header divider.

        Programmatic resizes flip ``_adjusting_name_width`` to skip the flag
        update; everything else (cursor drag, double-click-to-fit) flags the
        column as user-owned and stops the auto-grow.
        """
        if self._adjusting_name_width:
            return
        if logical_index == _COL_NAME:
            self._name_user_resized = True

    def _adjust_name_column(self) -> None:
        """Re-size Name to fill the leftover viewport width.

        No-op once the user has manually dragged Name (we respect their
        choice). Otherwise: viewport width minus the sum of every other
        column, clamped to a floor of 80px so Name never disappears even
        when a very long Path runs the table off-screen (horizontal
        scrollbar picks up the overflow).
        """
        if self._name_user_resized:
            return
        viewport_width = self.view.viewport().width()
        if viewport_width <= 0:
            return  # not yet laid out
        used = sum(
            self.view.columnWidth(i)
            for i in range(len(COLUMNS))
            if i != _COL_NAME
        )
        target = max(viewport_width - used, 80)
        if target == self.view.columnWidth(_COL_NAME):
            return
        self._adjusting_name_width = True
        try:
            self.view.setColumnWidth(_COL_NAME, target)
        finally:
            self._adjusting_name_width = False

    def set_collection_context(self, in_collection: bool) -> None:
        """Tell the table whether the current view is filtered to a collection."""
        self._collection_context = bool(in_collection)

    def set_available_collections(
        self, collections: list[tuple[int, str]]
    ) -> None:
        """Provide (id, name) pairs of user collections for the context menu."""
        self._available_collections = list(collections)

    def selected_rom_id(self) -> int | None:
        """Return the rom_id for the currently-selected row, or None."""
        index = self.view.selectionModel().currentIndex()
        if not index.isValid():
            return None
        source_index = self.proxy.mapToSource(index)
        if not source_index.isValid():
            return None
        row = self.model.row_at(source_index.row())
        return row.rom_id

    def selected_game_id(self) -> int | None:
        """Deprecated alias for :meth:`selected_rom_id` kept for compatibility."""
        return self.selected_rom_id()

    def select_rom(self, rom_id: int) -> bool:
        """Select the row whose ``RomRow.rom_id`` matches *rom_id*.

        Returns True on hit, False when the ROM is no longer in the
        current view (e.g. the user changed the system filter).

        Used by :meth:`MainWindow.refresh_all` to keep the user's
        selection stable across worker-triggered refreshes — without
        this, every Enrich / Find Covers / Scan run would drop the
        selection because ``set_rows`` rebuilds the model.
        """
        for source_row in range(self.model.rowCount()):
            row = self.model.row_at(source_row)
            if row.rom_id != rom_id:
                continue
            source_index = self.model.index(source_row, 0)
            proxy_index = self.proxy.mapFromSource(source_index)
            if proxy_index.isValid():
                self.view.setCurrentIndex(proxy_index)
                # ``setCurrentIndex`` only updates current; we also need
                # the selection model's selected-rows state so the row
                # is visually highlighted.
                self.view.selectionModel().select(
                    proxy_index,
                    self.view.selectionModel().SelectionFlag.Rows
                    | self.view.selectionModel().SelectionFlag.Select,
                )
                self.view.scrollTo(proxy_index)
                return True
            return False
        return False

    def select_game(self, rom_id: int) -> bool:
        """Deprecated alias for :meth:`select_rom` kept for compatibility."""
        return self.select_rom(rom_id)

    def _on_current_row_changed(
        self, current: QModelIndex, _previous: QModelIndex
    ) -> None:
        if not current.isValid():
            self.rom_selected.emit(None)
            return
        source_index = self.proxy.mapToSource(current)
        if not source_index.isValid():
            self.rom_selected.emit(None)
            return
        row = self.model.row_at(source_index.row())
        self.rom_selected.emit(row.rom_id)

    def _selected_row(self) -> RomRow | None:
        """Return the full RomRow for the currently-selected row, or None."""
        index = self.view.selectionModel().currentIndex()
        if not index.isValid():
            return None
        source_index = self.proxy.mapToSource(index)
        if not source_index.isValid():
            return None
        return self.model.row_at(source_index.row())

    def _on_context_menu(self, point: object) -> None:
        # Resolve the row under the cursor rather than the previously-
        # selected row. Right-clicking in Qt does NOT auto-select the
        # underlying row (only left-click does), so the old path returned
        # None whenever the user right-clicked without left-clicking first
        # — the menu silently didn't appear and looked like a regression.
        proxy_index = self.view.indexAt(point)  # type: ignore[arg-type]
        logger.debug(
            "context-menu: point=%s proxy_index_valid=%s proxy_row=%s",
            point,
            proxy_index.isValid(),
            proxy_index.row() if proxy_index.isValid() else None,
        )
        if not proxy_index.isValid():
            return
        source_index = self.proxy.mapToSource(proxy_index)
        if not source_index.isValid():
            logger.debug(
                "context-menu: proxy.mapToSource returned invalid index "
                "(proxy row %d)",
                proxy_index.row(),
            )
            return
        row = self.model.row_at(source_index.row())
        rom_id = row.rom_id
        logger.debug(
            "context-menu: source_row=%d rom_id=%s name=%r",
            source_index.row(),
            rom_id,
            row.name,
        )

        # Mirror left-click behaviour: promote the right-clicked row to
        # current so the detail panel + any subsequent action operate on
        # the row the user actually clicked, not whatever was selected
        # before.
        self.view.setCurrentIndex(proxy_index)
        menu = QMenu(self.view)

        fav_action = QAction("Add to Favorites", menu)
        fav_action.triggered.connect(
            lambda: self.add_to_favorites_requested.emit(rom_id)
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
                lambda: self.remove_from_collection_requested.emit(rom_id)
            )
            menu.addAction(remove_action)

        # Scoped actions — operate on this ROM only.
        menu.addSeparator()

        # Always enabled — clicking this opens the same options dialog
        # as the batch paths (Tools > Enrich Metadata, system right-click,
        # collection right-click), scoped to this one ROM. The user
        # picks fuzzy / re-enrich-existing / online via the dialog's
        # checkboxes; we don't hard-code the flags from this entry point.
        enrich_action = QAction("Enrich this ROM", menu)
        enrich_action.triggered.connect(
            lambda: self.enrich_rom_requested.emit(rom_id)
        )
        menu.addAction(enrich_action)

        heavy_scan_action = QAction("Heavy Scan this ROM", menu)
        heavy_scan_action.triggered.connect(
            lambda: self.heavy_scan_rom_requested.emit(rom_id)
        )
        menu.addAction(heavy_scan_action)

        covers_action = QAction("Find covers for this ROM", menu)
        covers_action.triggered.connect(
            lambda: self.find_local_covers_rom_requested.emit(rom_id)
        )
        menu.addAction(covers_action)

        # File-system actions — "Reveal in Explorer" and "Delete" both
        # operate on the specific ROM row the user right-clicked.
        menu.addSeparator()

        reveal_action = QAction("Reveal in Explorer", menu)
        reveal_action.setToolTip(
            "Open the file manager with this ROM highlighted."
        )
        reveal_action.triggered.connect(
            lambda: self.reveal_rom_requested.emit(rom_id)
        )
        menu.addAction(reveal_action)

        delete_action = QAction("Delete this ROM (permanent)…", menu)
        delete_action.setToolTip(
            "Permanently delete this ROM file from disk AND remove its "
            "library entry. There is no undo."
        )
        delete_action.triggered.connect(
            lambda: self.delete_rom_requested.emit(rom_id)
        )
        menu.addAction(delete_action)

        menu.exec(self.view.viewport().mapToGlobal(point))

    def _on_new_collection_request(self) -> None:
        name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
        if not ok:
            return
        trimmed = name.strip()
        if not trimmed:
            return
        self.new_collection_requested.emit(trimmed)
