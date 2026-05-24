"""Post-apply per-system summary dialog used by Export and Sync.

Both engines now populate a ``per_system`` mapping on their summary
object — :class:`romulus.core.exporter.ExportSummary` and
:class:`romulus.core.sync.SyncSummary` respectively. This dialog renders
that breakdown as a sortable table with one row per system plus a
totals row, so the user can see at a glance which systems contributed
what to the run.

Two factory classmethods, :meth:`PerSystemSummaryDialog.for_export` and
:meth:`PerSystemSummaryDialog.for_sync`, build the right column spec
for each engine. The dialog itself is engine-agnostic — it takes a
generic column-spec + rows-by-system dict.

Counts only — no per-file drill-down. The aggregate user need was "tell
me which systems succeeded vs failed and why," not "show me 17,000
filenames" — that would balloon the dialog and is already searchable
via ``logs/romulus.log``.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from romulus.core.exporter import ExportSummary, PerSystemExportCounts
from romulus.core.sync import PerSystemSyncCounts, SyncSummary


@dataclass(slots=True)
class _Column:
    """One column in the per-system summary table.

    ``key`` is the dataclass attribute name to read off the bucket
    (``PerSystemExportCounts`` or ``PerSystemSyncCounts``). ``label``
    is the header text shown to the user. ``is_bytes`` switches the
    cell formatter to ``_format_bytes``. ``error_like`` styles the
    cell red when non-zero so failures stand out at a glance.
    """

    key: str
    label: str
    is_bytes: bool = False
    error_like: bool = False


def _format_bytes(n: int) -> str:
    """Compact human-readable bytes — matches the exporter helper output."""
    if n <= 0:
        return "0"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{n} B"  # pragma: no cover - unreachable, defensive


class PerSystemSummaryDialog(QDialog):
    """Generic per-system summary table for Export / Sync apply results."""

    def __init__(
        self,
        *,
        title: str,
        intro: str,
        columns: list[_Column],
        rows_by_system: dict[str, object],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(820, 520)
        self._columns = columns

        layout = QVBoxLayout(self)

        intro_label = QLabel(intro, self)
        intro_label.setWordWrap(True)
        intro_label.setStyleSheet("color: #888; padding: 4px 0;")
        layout.addWidget(intro_label)

        # Sort systems by total work (sum of all numeric columns) descending,
        # so the systems that mattered most surface first. Ties broken by id.
        def _row_total(bucket: object) -> int:
            total = 0
            for col in columns:
                if col.is_bytes:
                    continue
                total += int(getattr(bucket, col.key, 0) or 0)
            return total

        sorted_systems = sorted(
            rows_by_system.items(),
            key=lambda item: (-_row_total(item[1]), item[0]),
        )

        header_labels = ["System", *(c.label for c in columns)]
        self._table = QTableWidget(
            len(sorted_systems) + 1, len(header_labels), self
        )
        self._table.setHorizontalHeaderLabels(header_labels)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Sortable on every column EXCEPT after the totals row is appended —
        # Qt's built-in sort would interleave the totals into the data. Lock
        # sorting off; users can scan the small N-systems table directly.
        self._table.setSortingEnabled(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for i in range(1, len(header_labels)):
            header.setSectionResizeMode(
                i, QHeaderView.ResizeMode.ResizeToContents
            )

        totals: dict[str, int] = {col.key: 0 for col in columns}
        for row_idx, (system_id, bucket) in enumerate(sorted_systems):
            self._table.setItem(row_idx, 0, _make_cell(system_id, bold=False))
            for col_idx, col in enumerate(columns, start=1):
                value = int(getattr(bucket, col.key, 0) or 0)
                totals[col.key] += value
                cell = _make_count_cell(value, col)
                self._table.setItem(row_idx, col_idx, cell)

        # Totals row at the bottom — separated visually by bold font.
        totals_row = len(sorted_systems)
        self._table.setItem(
            totals_row, 0, _make_cell("Totals", bold=True)
        )
        for col_idx, col in enumerate(columns, start=1):
            cell = _make_count_cell(totals[col.key], col, bold=True)
            self._table.setItem(totals_row, col_idx, cell)

        layout.addWidget(self._table, stretch=1)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, self
        )
        button_box.rejected.connect(self.reject)
        button_box.accepted.connect(self.accept)
        button_box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            self.accept
        )
        layout.addWidget(button_box)

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def for_export(
        cls, summary: ExportSummary, parent: QWidget | None = None
    ) -> PerSystemSummaryDialog:
        """Build a summary dialog from an :class:`ExportSummary`.

        Columns mirror the exporter's skip-reason taxonomy: copied
        (with bytes), covers refreshed (sidecar phase), already-present
        (idempotent re-runs), unsupported (profile said no), refused
        (security guard), errors (sidecar failures / FK / etc).

        The "Covers refreshed" column is what makes the artwork-only
        mode legible — every other counter is 0 in that mode, so
        without this column the per-system summary would just be a
        table of empty rows. Even in normal full-export mode it
        surfaces the cover-copy work that used to be invisible in the
        aggregate ``artwork_copied`` total.

        The "Skipped duplicates" column is added conditionally — only when
        at least one system has a non-zero count — so the dialog stays tight
        for the common case where ``distinct_content_only`` is off.
        """
        rows_by_system: dict[str, object] = dict(summary.per_system.items())
        # If a system somehow ended up with zero rows in per_system but is
        # listed in ``systems`` (shouldn't happen, but defensive), add a
        # blank bucket so it still shows up.
        for system_id in summary.systems:
            rows_by_system.setdefault(system_id, PerSystemExportCounts())

        # Only render the skipped_duplicates column when the distinct-content
        # filter actually ran and produced skips.
        any_skipped_dupes = any(
            int(getattr(bucket, "skipped_duplicates", 0) or 0) > 0
            for bucket in rows_by_system.values()
        )
        columns = [
            _Column("copied", "Copied"),
            _Column("bytes_copied", "Bytes", is_bytes=True),
            _Column("artwork_copied", "Covers refreshed"),
            _Column("skipped_already_present", "Already on dest"),
            _Column("skipped_unsupported", "Unsupported"),
            _Column("skipped_refused", "Refused", error_like=True),
            _Column("errors", "Errors", error_like=True),
        ]
        if any_skipped_dupes:
            columns.append(_Column("skipped_duplicates", "Skipped (dup)"))

        intro = (
            f"Exported {summary.files_copied} file(s) "
            f"({_format_bytes(summary.bytes_copied)}) across "
            f"{len(summary.systems)} system(s) — broken down below. "
            f"Skipped {summary.files_skipped} file(s); "
            f"{len(summary.errors)} error(s) total."
        )
        return cls(
            title="Export complete — per-system breakdown",
            intro=intro,
            columns=columns,
            rows_by_system=rows_by_system,
            parent=parent,
        )

    @classmethod
    def for_sync(
        cls, summary: SyncSummary, parent: QWidget | None = None
    ) -> PerSystemSummaryDialog:
        """Build a summary dialog from a :class:`SyncSummary`.

        Sync's columns differ from export's because sync can move bytes
        in either direction and can delete on either side. Identical
        actions show up as their own column rather than being lumped
        with copy-skips.
        """
        columns = [
            _Column("copied_to_dest", "Copied → dest"),
            _Column("copied_to_local", "Pulled → local"),
            _Column("deleted_dest", "Deleted (dest)", error_like=False),
            _Column("deleted_local", "Deleted (local)", error_like=False),
            _Column("skipped_identical", "Already identical"),
            _Column("bytes_copied", "Bytes moved", is_bytes=True),
            _Column("failed", "Failed", error_like=True),
        ]
        rows_by_system: dict[str, object] = dict(summary.per_system.items())
        for system_id in summary.systems_touched:
            rows_by_system.setdefault(system_id, PerSystemSyncCounts())
        copied_total = (
            summary.files_added_to_dest + summary.files_pulled_to_local
        )
        deleted_total = summary.files_removed_from_dest
        intro = (
            f"Sync applied {summary.applied} action(s) across "
            f"{len(summary.systems_touched)} system(s). "
            f"Copied {copied_total}, deleted {deleted_total}, "
            f"skipped {summary.skipped} identical, "
            f"failed {summary.failed}."
        )
        return cls(
            title="Sync complete — per-system breakdown",
            intro=intro,
            columns=columns,
            rows_by_system=rows_by_system,
            parent=parent,
        )


# ---------------------------------------------------------------------------
# Cell formatters — kept module-private; only the dialog uses them.
# ---------------------------------------------------------------------------


def _make_cell(text: str, *, bold: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


def _make_count_cell(
    value: int, col: _Column, *, bold: bool = False
) -> QTableWidgetItem:
    """Build a table cell for a numeric column.

    Renders bytes columns through :func:`_format_bytes`, others as
    decimal. Non-zero error-like cells get a red foreground so the
    user can scan for problems without reading every number.
    """
    text = _format_bytes(value) if col.is_bytes else f"{value:,}"
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    item.setTextAlignment(
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    )
    if bold:
        font: QFont = item.font()
        font.setBold(True)
        item.setFont(font)
    if col.error_like and value > 0:
        item.setForeground(QBrush(QColor("#c0392b")))
    elif value == 0 and not bold:
        item.setForeground(QBrush(QColor("#888")))
    return item
