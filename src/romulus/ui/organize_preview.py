"""Organize preview dialog — before/after view with approve/reject checkboxes.

The dialog renders an :class:`~romulus.core.organizer.OrganizePlan` as a Qt tree
view grouped by action kind. Every leaf node carries a checkbox; users can
toggle individual actions or use the bulk Select All / Deselect All buttons.
Collisions render as a non-checkable section because they require manual
filesystem work the organizer can't safely do on its own.

When the user clicks Apply, the dialog emits :pyattr:`actions_approved` with
the list of approved :class:`OrganizeAction` instances. The caller is
responsible for executing the plan (typically via :class:`OrganizeWorker`).
"""

from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from romulus.core.organizer import (
    ACTION_COLLISION,
    ACTION_DELETE_DUPLICATE,
    ACTION_MERGE_FOLDER,
    ACTION_RENAME,
    OrganizeAction,
    OrganizePlan,
)

_ACTION_LABELS: dict[str, str] = {
    ACTION_MERGE_FOLDER: "Merge folders",
    ACTION_RENAME: "Renames",
    ACTION_DELETE_DUPLICATE: "Duplicate removals",
    ACTION_COLLISION: "Collisions (manual review)",
}

# Action role used to round-trip the OrganizeAction object through the model.
_ACTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class OrganizePreviewDialog(QDialog):
    """Preview/commit dialog for an :class:`OrganizePlan`."""

    #: Emitted with the list of approved actions when the user clicks Apply.
    actions_approved = Signal(list)

    def __init__(self, plan: OrganizePlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Organize Library")
        self.setModal(True)
        self.resize(800, 600)
        self._plan = plan

        layout = QVBoxLayout(self)

        # Summary header
        self._summary_label = QLabel(self._build_summary_text(), self)
        layout.addWidget(self._summary_label)

        # Tree
        self._tree = QTreeView(self)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(["Action", "Source", "Target"])
        self._populate_model()
        self._tree.setModel(self._model)
        self._tree.expandAll()
        self._tree.setColumnWidth(0, 220)
        self._tree.setColumnWidth(1, 260)
        layout.addWidget(self._tree)

        # Friendlier UX: when the plan is empty the tree view occupies most
        # of the dialog with no content. Show a centred placeholder so users
        # see "All clean, nothing to do" instead of a blank space.
        if not self._plan.actions:
            self._empty_placeholder = QLabel(
                "All clean — your library is already organized.\n"
                "Nothing to do.",
                self,
            )
            self._empty_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._empty_placeholder.setStyleSheet("color: #888; padding: 16px;")
            self._tree.setVisible(False)
            layout.addWidget(self._empty_placeholder, 1)

        # Select all / deselect all
        select_row = QHBoxLayout()
        self._select_all_btn = QPushButton("Select All", self)
        self._select_all_btn.clicked.connect(self._on_select_all)
        select_row.addWidget(self._select_all_btn)
        self._deselect_all_btn = QPushButton("Deselect All", self)
        self._deselect_all_btn.clicked.connect(self._on_deselect_all)
        select_row.addWidget(self._deselect_all_btn)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        # Progress bar (hidden until Apply is clicked).
        self._progress = QProgressBar(self)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Apply / Cancel
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._apply_btn = button_box.button(QDialogButtonBox.StandardButton.Apply)
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        if not self._plan.actions:
            self._apply_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Build helpers
    # ------------------------------------------------------------------

    def _build_summary_text(self) -> str:
        counts = self._plan.counts_by_kind()
        renames = counts.get(ACTION_RENAME, 0)
        merges = counts.get(ACTION_MERGE_FOLDER, 0)
        dupes = counts.get(ACTION_DELETE_DUPLICATE, 0)
        collisions = counts.get(ACTION_COLLISION, 0)
        if not self._plan.actions:
            return "Library is already organized — no changes needed."
        parts = [
            f"{renames} file(s) to rename",
            f"{merges} folder(s) to merge",
            f"{dupes} duplicate(s) to remove",
        ]
        text = ", ".join(parts) + "."
        if collisions:
            text += f"  {collisions} collision(s) need manual review."
        return text

    def _populate_model(self) -> None:
        """Build a section per action kind, with a checkable leaf per action."""
        # Group actions by kind in a stable order.
        order = [
            ACTION_MERGE_FOLDER,
            ACTION_RENAME,
            ACTION_DELETE_DUPLICATE,
            ACTION_COLLISION,
        ]
        by_kind: defaultdict[str, list[OrganizeAction]] = defaultdict(list)
        for action in self._plan.actions:
            by_kind[action.kind].append(action)
        root = self._model.invisibleRootItem()
        for kind in order:
            actions = by_kind.get(kind, [])
            if not actions:
                continue
            header = QStandardItem(f"{_ACTION_LABELS[kind]} ({len(actions)})")
            header.setEditable(False)
            header.setSelectable(False)
            for action in actions:
                checkbox_item = QStandardItem(action.reason or kind)
                checkbox_item.setEditable(False)
                checkbox_item.setData(action, _ACTION_ROLE)
                if kind == ACTION_COLLISION:
                    # Collisions can't be auto-applied; surface read-only.
                    checkbox_item.setCheckable(False)
                else:
                    checkbox_item.setCheckable(True)
                    checkbox_item.setCheckState(Qt.CheckState.Checked)
                source_item = QStandardItem(action.source_path)
                source_item.setEditable(False)
                target_item = QStandardItem(action.target_path)
                target_item.setEditable(False)
                header.appendRow([checkbox_item, source_item, target_item])
            root.appendRow(header)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _iter_action_items(self) -> list[QStandardItem]:
        items: list[QStandardItem] = []
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            header = root.child(i, 0)
            if header is None:
                continue
            for j in range(header.rowCount()):
                child = header.child(j, 0)
                if child is not None and child.isCheckable():
                    items.append(child)
        return items

    def _on_select_all(self) -> None:
        for item in self._iter_action_items():
            item.setCheckState(Qt.CheckState.Checked)

    def _on_deselect_all(self) -> None:
        for item in self._iter_action_items():
            item.setCheckState(Qt.CheckState.Unchecked)

    def approved_actions(self) -> list[OrganizeAction]:
        """Return every action whose checkbox is currently checked."""
        out: list[OrganizeAction] = []
        for item in self._iter_action_items():
            if item.checkState() == Qt.CheckState.Checked:
                action = item.data(_ACTION_ROLE)
                if isinstance(action, OrganizeAction):
                    out.append(action)
        return out

    def _on_apply_clicked(self) -> None:
        """Switch into 'executing' mode and emit ``actions_approved``."""
        approved = self.approved_actions()
        if not approved:
            self.reject()
            return
        self._select_all_btn.setEnabled(False)
        self._deselect_all_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, len(approved))
        self._progress.setValue(0)
        self.actions_approved.emit(approved)

    # ------------------------------------------------------------------
    # Progress hooks used by the caller while a worker runs.
    # ------------------------------------------------------------------

    def on_progress(self, current: int, total: int, source: str) -> None:
        """Slot — drive the progress bar from worker ticks."""
        if total > 0:
            self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._summary_label.setText(
            f"Applying {current} of {total}: {source}"
        )

    def on_finished(self, applied: int, skipped: int, failed: int) -> None:
        """Slot — show the post-apply summary and switch Cancel into Close."""
        self._summary_label.setText(
            f"Done. Applied {applied}, skipped {skipped}, failed {failed}."
        )
        self._progress.setValue(self._progress.maximum())

    def on_failed(self, message: str) -> None:
        """Slot — show an error message."""
        self._summary_label.setText(message)
