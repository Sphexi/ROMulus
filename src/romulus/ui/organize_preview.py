"""Organize preview dialog — before/after view with approve/reject checkboxes.

The dialog renders an :class:`~romulus.core.organizer.OrganizePlan` as a Qt tree
view grouped by action kind. Every checkable leaf carries a checkbox; users can
toggle individual actions or use the bulk Select All / Deselect All buttons.
Collision rows render with a per-row resolution dropdown ("Do nothing" by
default; "Delete source" or "Delete target and rename source" when the case-3
detector captured both source and target rom IDs). When the user clicks Apply,
each collision's resolution is expanded into concrete actions via
:func:`romulus.core.organizer.resolve_collision`.

When the user clicks Apply, the dialog emits :pyattr:`actions_approved` with
the resolved list of approved :class:`OrganizeAction` instances. The caller is
responsible for executing the plan (typically via :class:`OrganizeWorker`).
"""

from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
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
    RESOLUTION_DO_NOTHING,
    OrganizeAction,
    OrganizePlan,
    available_resolutions,
    resolve_collision,
)
from romulus.ui._grouped_tree import GroupedCheckboxTreeMixin

_ACTION_LABELS: dict[str, str] = {
    ACTION_MERGE_FOLDER: "Folder merges",
    ACTION_RENAME: "DAT-verified renames",
    ACTION_DELETE_DUPLICATE: "Hash duplicate removals",
    ACTION_COLLISION: "Collisions (manual review)",
}

# Action role used to round-trip the OrganizeAction object through the model.
_ACTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class OrganizePreviewDialog(QDialog, GroupedCheckboxTreeMixin):
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

        # Per-collision resolution combo boxes. Keyed by id(action) so we
        # can look up the user's selection at Apply time. Only populated
        # for ACTION_COLLISION rows.
        self._collision_combos: dict[int, QComboBox] = {}

        # Tree
        self._tree = QTreeView(self)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(
            ["Action", "Source", "Target", "Resolution"]
        )
        self._populate_model()
        self._tree.setModel(self._model)
        self._install_group_toggle()
        self._tree.expandAll()
        self._tree.setColumnWidth(0, 220)
        self._tree.setColumnWidth(1, 260)
        self._tree.setColumnWidth(2, 260)
        self._tree.setColumnWidth(3, 240)
        # Embed the dropdowns AFTER setModel + expandAll so the indices exist.
        self._install_collision_combos()
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
            f"{renames} DAT-verified rename(s)",
            f"{merges} folder merge(s)",
            f"{dupes} hash duplicate(s) to remove",
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
                    # Collisions can't be auto-applied via checkbox; user
                    # picks an explicit resolution in the 4th column.
                    checkbox_item.setCheckable(False)
                else:
                    checkbox_item.setCheckable(True)
                    checkbox_item.setCheckState(Qt.CheckState.Checked)
                source_item = QStandardItem(action.source_path)
                source_item.setEditable(False)
                target_item = QStandardItem(action.target_path)
                target_item.setEditable(False)
                # Fourth column — populated post-setModel by
                # ``_install_collision_combos`` for collision rows;
                # remains blank for everything else.
                resolution_item = QStandardItem("")
                resolution_item.setEditable(False)
                header.appendRow(
                    [checkbox_item, source_item, target_item, resolution_item]
                )
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

    def _install_collision_combos(self) -> None:
        """Embed a resolution QComboBox in column 3 for each collision row.

        Combos can only be installed after ``setModel`` (the QTreeView needs
        valid indices) AND after ``expandAll`` (so the child rows are part
        of the visible layout — collapsed children don't get widgets). We
        keep a reference to each combo in ``self._collision_combos`` keyed
        by ``id(action)`` so :meth:`approved_actions` can read each user
        selection without walking the model again.
        """
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            header = root.child(i, 0)
            if header is None:
                continue
            for j in range(header.rowCount()):
                child = header.child(j, 0)
                if child is None:
                    continue
                action = child.data(_ACTION_ROLE)
                if not isinstance(action, OrganizeAction):
                    continue
                if action.kind != ACTION_COLLISION:
                    continue
                combo = QComboBox(self._tree)
                for value, label in available_resolutions(action):
                    combo.addItem(label, value)
                # Default: "Do nothing" (always first option).
                combo.setCurrentIndex(0)
                # Place the combo at column 3 of this child row.
                resolution_idx = self._model.indexFromItem(
                    header.child(j, 3)
                )
                self._tree.setIndexWidget(resolution_idx, combo)
                self._collision_combos[id(action)] = combo

    def approved_actions(self) -> list[OrganizeAction]:
        """Return every action approved for execution.

        For checkable kinds (rename / dedup / merge): include any action
        whose checkbox is checked.

        For collision rows: read the resolution combo box and expand via
        :func:`romulus.core.organizer.resolve_collision`. "Do nothing"
        yields zero actions; the other choices yield one or two concrete
        actions per collision.
        """
        out: list[OrganizeAction] = []
        # Pass 1 — checkbox-approved actions in their original order.
        for item in self._iter_action_items():
            if item.checkState() == Qt.CheckState.Checked:
                action = item.data(_ACTION_ROLE)
                if isinstance(action, OrganizeAction):
                    out.append(action)
        # Pass 2 — walk collision rows, expand each user-chosen resolution.
        for action, combo in self._iter_collision_combos():
            resolution = combo.currentData()
            if not resolution or resolution == RESOLUTION_DO_NOTHING:
                continue
            out.extend(resolve_collision(action, str(resolution)))
        return out

    def _iter_collision_combos(self) -> list[tuple[OrganizeAction, QComboBox]]:
        """Yield (collision_action, combo) pairs in model order."""
        pairs: list[tuple[OrganizeAction, QComboBox]] = []
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            header = root.child(i, 0)
            if header is None:
                continue
            for j in range(header.rowCount()):
                child = header.child(j, 0)
                if child is None:
                    continue
                action = child.data(_ACTION_ROLE)
                if not isinstance(action, OrganizeAction):
                    continue
                if action.kind != ACTION_COLLISION:
                    continue
                combo = self._collision_combos.get(id(action))
                if combo is not None:
                    pairs.append((action, combo))
        return pairs

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

    def on_finished(
        self, applied: int, skipped: int, failed: int, errors: list[str] | None = None  # noqa: ARG002
    ) -> None:
        """Slot — fill the bar to 100% and show the post-apply summary."""
        self._progress.setRange(0, max(1, self._progress.maximum()))
        self._progress.setValue(self._progress.maximum())
        icon = "✓" if failed == 0 else "✗"
        self._summary_label.setText(
            f"{icon} Done. Applied {applied}, skipped {skipped}, failed {failed}."
        )

    def on_failed(self, message: str) -> None:
        """Slot — fill bar to end, show an error message."""
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._summary_label.setText(f"✗ {message}")
