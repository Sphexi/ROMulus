"""Verify Library preview dialog — grouped checkbox tree per scrub bucket.

Renders a :class:`romulus.core.scrub.ScrubPlan` as a Qt tree view grouped
by mismatch type. Every leaf node carries a checkbox; users can toggle
individual actions or use the bulk Select All / Deselect All buttons.

Defaults are conservative:

* ``missing_unflagged`` and ``flagged_but_present`` — checked by default;
  these are no-data-loss fix-ups.
* ``outside_root`` — UNCHECKED by default. These rows belong to a
  library the user has switched away from; deleting them is destructive
  and the user might still want to switch back. Force the user to opt in.
* ``drift`` — UNCHECKED by default. File contents have changed under
  ROMulus; the right next action is usually Heavy Scan, not a forced
  hash invalidation.

When the user clicks Apply, the dialog emits :pyattr:`actions_approved`
with the list of approved :class:`ScrubAction` instances. The caller
runs :class:`ScrubApplyWorker` against them.
"""

from __future__ import annotations

import contextlib
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

from romulus.core.scrub import ScrubAction, ScrubPlan, ScrubStatus
from romulus.ui._grouped_tree import GroupedCheckboxTreeMixin

#: Display label + default-checked state per scrub bucket. The order of
#: the dict is the order the sections render in the tree.
_BUCKET_DEFAULTS: dict[ScrubStatus, tuple[str, bool]] = {
    "missing_unflagged": ("Missing on disk (will tombstone)", True),
    "flagged_but_present": ("Flagged missing but present (will un-tombstone)", True),
    "outside_root": (
        "Outside current library (will DELETE — review carefully)",
        False,
    ),
    "drift": ("Size/mtime drift (will invalidate hash + restat)", False),
}

# Action role used to round-trip the ScrubAction object through the model.
_ACTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class ScrubPreviewDialog(QDialog, GroupedCheckboxTreeMixin):
    """Preview / commit dialog for a :class:`ScrubPlan`."""

    actions_approved = Signal(list)

    def __init__(self, plan: ScrubPlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Verify Library")
        self.setModal(True)
        self.resize(900, 600)
        self._plan = plan

        layout = QVBoxLayout(self)

        self._summary_label = QLabel(self._build_summary_text(), self)
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        self._tree = QTreeView(self)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(
            ["Mismatch", "Path", "Detail"]
        )
        self._populate_model()
        self._tree.setModel(self._model)
        self._install_group_toggle()
        self._tree.expandAll()
        self._tree.setColumnWidth(0, 280)
        self._tree.setColumnWidth(1, 360)
        layout.addWidget(self._tree)

        # Empty-plan placeholder.
        if not self._plan.actions:
            placeholder = QLabel(
                "All clean — every database row matches the disk.\n"
                "Nothing to do.",
                self,
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #888; padding: 16px;")
            self._tree.setVisible(False)
            layout.addWidget(placeholder, 1)

        select_row = QHBoxLayout()
        self._select_all_btn = QPushButton("Select All", self)
        self._select_all_btn.clicked.connect(self._on_select_all)
        select_row.addWidget(self._select_all_btn)
        self._deselect_all_btn = QPushButton("Deselect All", self)
        self._deselect_all_btn.clicked.connect(self._on_deselect_all)
        select_row.addWidget(self._deselect_all_btn)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        self._progress = QProgressBar(self)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

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
        if not self._plan.actions and self._plan.rows_unreadable == 0:
            return (
                f"Scanned {self._plan.rows_scanned} rows — every one matches "
                f"the disk under the current library."
            )
        counts = self._plan.counts_by_status()
        parts = [
            f"{counts.get(status, 0)} {label.lower().split(' (')[0]}"
            for status, (label, _default) in _BUCKET_DEFAULTS.items()
        ]
        text = (
            f"Scanned {self._plan.rows_scanned} rows. "
            + " · ".join(parts)
            + "."
        )
        if self._plan.rows_unreadable:
            text += (
                f"\n{self._plan.rows_unreadable} row(s) could not be checked "
                f"(drive offline / permission denied) — re-run when the "
                f"share is available."
            )
        return text

    def _populate_model(self) -> None:
        """Build a section per bucket, with a checkable leaf per action."""
        by_bucket: defaultdict[ScrubStatus, list[ScrubAction]] = defaultdict(list)
        for action in self._plan.actions:
            by_bucket[action.status].append(action)
        root = self._model.invisibleRootItem()
        for status, (label, default_checked) in _BUCKET_DEFAULTS.items():
            actions = by_bucket.get(status, [])
            if not actions:
                continue
            header = QStandardItem(f"{label}  ({len(actions)})")
            header.setEditable(False)
            header.setSelectable(False)
            for action in actions:
                checkbox_item = QStandardItem(action.filename)
                checkbox_item.setEditable(False)
                checkbox_item.setData(action, _ACTION_ROLE)
                checkbox_item.setCheckable(True)
                checkbox_item.setCheckState(
                    Qt.CheckState.Checked
                    if default_checked
                    else Qt.CheckState.Unchecked
                )
                path_item = QStandardItem(action.path)
                path_item.setEditable(False)
                detail_item = QStandardItem(_format_detail(action))
                detail_item.setEditable(False)
                header.appendRow([checkbox_item, path_item, detail_item])
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

    def approved_actions(self) -> list[ScrubAction]:
        out: list[ScrubAction] = []
        for item in self._iter_action_items():
            if item.checkState() == Qt.CheckState.Checked:
                action = item.data(_ACTION_ROLE)
                if isinstance(action, ScrubAction):
                    out.append(action)
        return out

    def _on_apply_clicked(self) -> None:
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

    def on_progress(self, current: int, total: int, label: str) -> None:
        if total > 0:
            self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._summary_label.setText(f"Applying {current} of {total}: {label}")

    def on_finished(
        self,
        flagged_missing: int,
        deleted_outside_root: int,
        untombstoned: int,
        drift_fixed: int,
        pruned_games: int,
        errors: list[str],
    ) -> None:
        self._progress.setRange(0, max(1, self._progress.maximum()))
        self._progress.setValue(self._progress.maximum())
        icon = "✓" if not errors else "✗"
        bits = []
        if flagged_missing:
            bits.append(f"flagged {flagged_missing} missing")
        if deleted_outside_root:
            bits.append(f"deleted {deleted_outside_root} outside-library")
        if untombstoned:
            bits.append(f"un-tombstoned {untombstoned}")
        if drift_fixed:
            bits.append(f"reset {drift_fixed} drifted")
        if pruned_games:
            bits.append(f"pruned {pruned_games} orphan games")
        body = ", ".join(bits) if bits else "no changes"
        err_line = (
            f"\n{len(errors)} bucket(s) failed — see logs/romulus.log."
            if errors
            else ""
        )
        self._summary_label.setText(f"{icon} Done. {body}.{err_line}")
        self._apply_btn.setText("Close")
        self._apply_btn.setEnabled(True)
        with contextlib.suppress(TypeError, RuntimeError):
            self._apply_btn.clicked.disconnect(self._on_apply_clicked)
        self._apply_btn.clicked.connect(self.accept)

    def on_failed(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._summary_label.setText(f"✗ {message}")
        self._apply_btn.setText("Close")
        self._apply_btn.setEnabled(True)
        with contextlib.suppress(TypeError, RuntimeError):
            self._apply_btn.clicked.disconnect(self._on_apply_clicked)
        self._apply_btn.clicked.connect(self.accept)


def _format_detail(action: ScrubAction) -> str:
    """Build the third-column 'detail' string for an action row."""
    if action.status == "outside_root" and action.library_root:
        return f"library_root = {action.library_root}"
    if action.status == "drift":
        stored = f"{action.stored_size}B @ {action.stored_mtime:.0f}"
        current = (
            f"{action.current_size}B @ {action.current_mtime:.0f}"
            if action.current_size is not None and action.current_mtime is not None
            else "?"
        )
        return f"stored {stored} → disk {current}"
    return ""
