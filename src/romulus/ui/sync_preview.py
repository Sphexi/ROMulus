"""Sync preview dialog — bucketed action tree with per-row checkboxes.

Mirrors :mod:`romulus.ui.organize_preview` in structure (tree of action
buckets, checkable leaves, Apply / Cancel buttons) and adds the bits the
destination-sync feature needs on top:

* Bucketed counts in the header — to add / to remove / to pull / conflicts /
  already identical (last hidden by default per spec §6.2).
* Per-row conflict-policy dropdown for two-way mode. The plan's default
  policy is preselected; users can override per row.
* The destructive-action double-confirm sequence from spec §6.3. The first
  message details what's about to be added / deleted / overwritten; the
  second is a final "Are you sure?" gate. Both only fire when the plan
  contains any delete-or-overwrite action; non-destructive plans get a
  single Apply click.
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
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from romulus.core.sync import (
    ACTION_CONFLICT,
    ACTION_COPY_TO_DEST,
    ACTION_COPY_TO_LOCAL,
    ACTION_DELETE_DEST,
    ACTION_DELETE_LOCAL,
    ACTION_IDENTICAL,
    CONFLICT_RESOLUTION_DEST,
    CONFLICT_RESOLUTION_LOCAL,
    CONFLICT_RESOLUTION_NEWEST,
    CONFLICT_RESOLUTION_PROMPT,
    CONFLICT_RESOLUTION_SKIP,
    SyncAction,
    SyncPlan,
)

# Display labels for each bucket — keep in sync with the spec's preview mock.
_BUCKET_LABELS: dict[str, str] = {
    ACTION_COPY_TO_DEST: "To add to dest",
    ACTION_DELETE_DEST: "To remove from dest",
    ACTION_COPY_TO_LOCAL: "To pull to local",
    ACTION_DELETE_LOCAL: "To remove from local",
    ACTION_CONFLICT: "Conflicts",
    ACTION_IDENTICAL: "Already identical",
}

# Ordered list driving the tree's section order.
_BUCKET_ORDER: tuple[str, ...] = (
    ACTION_COPY_TO_DEST,
    ACTION_DELETE_DEST,
    ACTION_COPY_TO_LOCAL,
    ACTION_DELETE_LOCAL,
    ACTION_CONFLICT,
    ACTION_IDENTICAL,
)

#: Conflict-policy dropdown entries, in the order shown to the user.
_CONFLICT_POLICY_CHOICES: tuple[tuple[str, str], ...] = (
    ("Skip", CONFLICT_RESOLUTION_SKIP),
    ("Local wins", CONFLICT_RESOLUTION_LOCAL),
    ("Dest wins", CONFLICT_RESOLUTION_DEST),
    ("Newest mtime wins", CONFLICT_RESOLUTION_NEWEST),
    ("Prompt", CONFLICT_RESOLUTION_PROMPT),
)

_ACTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class SyncPreviewDialog(QDialog):
    """Preview / commit dialog for a :class:`SyncPlan`."""

    #: Emitted with the list of approved :class:`SyncAction` instances after
    #: the user clicks Apply AND any destructive-action prompts are accepted.
    actions_approved = Signal(list)

    def __init__(
        self,
        plan: SyncPlan,
        destination_label: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            f"Sync preview ({destination_label})" if destination_label else "Sync preview"
        )
        self.setModal(True)
        self.resize(900, 640)
        self._plan = plan
        self._destination_label = destination_label

        layout = QVBoxLayout(self)

        # Intro paragraph — added after user testing reported that the
        # original UI was unclear about what Apply actually does. Without
        # this people thought the dialog "just identified the diff" and
        # weren't sure whether files would actually move.
        self._intro_label = QLabel(
            "This preview shows the diff between your library and the "
            "destination. Check the actions you want to perform, then "
            'click "Apply changes to <target>" to copy/delete files. '
            'Files marked "Already identical" require no work. '
            "Nothing is written to disk until you click Apply.",
            self,
        )
        self._intro_label.setWordWrap(True)
        self._intro_label.setStyleSheet("color: #666; padding: 4px 0 8px 0;")
        layout.addWidget(self._intro_label)

        # Header row: bucketed counts (left) + size totals summary (right).
        header_row = QHBoxLayout()
        self._summary_label = QLabel(self._build_summary_text(), self)
        header_row.addWidget(self._summary_label, stretch=1)
        self._totals_label = QLabel(self._build_totals_text(), self)
        self._totals_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._totals_label.setStyleSheet(
            "color: #0a5; font-weight: bold; padding: 0 4px;"
        )
        header_row.addWidget(self._totals_label)
        layout.addLayout(header_row)

        # Tree view
        self._tree = QTreeView(self)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(
            ["Action", "Relative path", "Size", "Policy"]
        )
        self._populate_model()
        self._tree.setModel(self._model)
        # Conflict-policy dropdowns are inserted via setIndexWidget after the
        # model is attached so their parents are correct.
        self._install_conflict_widgets()
        self._tree.expandAll()
        self._tree.setColumnWidth(0, 240)
        self._tree.setColumnWidth(1, 360)
        self._tree.setColumnWidth(2, 100)
        layout.addWidget(self._tree)

        if not plan.actions:
            self._empty_placeholder = QLabel(
                "Destination already matches the local library — nothing to do.",
                self,
            )
            self._empty_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._empty_placeholder.setStyleSheet("color: #888; padding: 16px;")
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
        # Bake the destination into the button text so it's obvious where
        # the bytes are going — the generic "Apply" label was a major
        # source of user confusion ("is this just identifying the diff or
        # actually copying?").
        self._apply_btn.setText(self._apply_button_text())
        self._apply_btn.setToolTip(
            "Copy / delete the checked files now. The destructive prompts "
            "fire before any write."
        )
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        self._cancel_btn = button_box.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        if self._cancel_btn is not None:
            self._cancel_btn.setToolTip(
                "Discard the plan and close this dialog without writing "
                "anything to disk."
            )
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        if not plan.actions:
            self._apply_btn.setEnabled(False)

    def _apply_button_text(self) -> str:
        """Apply-button label that names the destination, truncated to ~60 chars."""
        target = self._destination_label or "destination"
        # Truncate aggressively so the button doesn't blow out the dialog
        # width on long network paths — keep the trailing path component
        # visible since that's what disambiguates similar mounts.
        max_inner = 60
        if len(target) > max_inner:
            target = "…" + target[-(max_inner - 1):]
        return f"Apply changes to {target}"

    # ------------------------------------------------------------------
    # Header text
    # ------------------------------------------------------------------

    def _build_summary_text(self) -> str:
        counts = self._plan.counts_by_kind()
        bytes_by_kind = self._plan.bytes_by_kind()
        parts: list[str] = []
        for bucket in _BUCKET_ORDER:
            n = counts.get(bucket, 0)
            if not n:
                continue
            label = _BUCKET_LABELS[bucket].lower()
            size = bytes_by_kind.get(bucket, 0)
            if size:
                parts.append(f"{n} {label} ({_format_bytes(size)})")
            else:
                parts.append(f"{n} {label}")
        if not parts:
            return "Destination is already in sync — nothing to do."
        return "  •  ".join(parts)

    def _build_totals_text(self) -> str:
        """Short top-right summary — copy / delete / unchanged counts."""
        counts = self._plan.counts_by_kind()
        bytes_by_kind = self._plan.bytes_by_kind()
        copies = counts.get(ACTION_COPY_TO_DEST, 0) + counts.get(
            ACTION_COPY_TO_LOCAL, 0
        )
        copy_bytes = bytes_by_kind.get(ACTION_COPY_TO_DEST, 0) + bytes_by_kind.get(
            ACTION_COPY_TO_LOCAL, 0
        )
        deletes = counts.get(ACTION_DELETE_DEST, 0) + counts.get(
            ACTION_DELETE_LOCAL, 0
        )
        unchanged = counts.get(ACTION_IDENTICAL, 0)
        parts: list[str] = []
        if copies:
            parts.append(
                f"{copies:,} file(s) ({_format_bytes(copy_bytes)}) to copy"
            )
        if deletes:
            parts.append(f"{deletes:,} file(s) to delete")
        if unchanged:
            parts.append(f"{unchanged:,} unchanged")
        if not parts:
            return ""
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # Tree population
    # ------------------------------------------------------------------

    def _populate_model(self) -> None:
        """Build a section per action kind, with checkable leaves per action."""
        by_kind: defaultdict[str, list[SyncAction]] = defaultdict(list)
        for action in self._plan.actions:
            by_kind[action.kind].append(action)
        root = self._model.invisibleRootItem()
        for kind in _BUCKET_ORDER:
            actions = by_kind.get(kind, [])
            if not actions:
                continue
            header_label = _BUCKET_LABELS[kind]
            header = QStandardItem(f"{header_label} ({len(actions)})")
            header.setEditable(False)
            header.setSelectable(False)
            for action in actions:
                check = QStandardItem("")
                check.setEditable(False)
                check.setCheckable(True)
                # Identical actions default unchecked (no-op anyway).
                # Destructive bucket actions default checked but the user
                # can deselect them individually.
                check.setCheckState(
                    Qt.CheckState.Unchecked
                    if kind == ACTION_IDENTICAL
                    else Qt.CheckState.Checked
                )
                check.setData(action, _ACTION_ROLE)
                rel_item = QStandardItem(action.rel_path or action.dest_path)
                rel_item.setEditable(False)
                size_item = QStandardItem(_format_bytes(action.size_bytes))
                size_item.setEditable(False)
                policy_placeholder = QStandardItem("")
                policy_placeholder.setEditable(False)
                header.appendRow([check, rel_item, size_item, policy_placeholder])
            root.appendRow(header)

    def _install_conflict_widgets(self) -> None:
        """Attach a QComboBox to the Policy column for every conflict row."""
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            header = root.child(i, 0)
            if header is None:
                continue
            for j in range(header.rowCount()):
                check_item = header.child(j, 0)
                if check_item is None:
                    continue
                action = check_item.data(_ACTION_ROLE)
                if not isinstance(action, SyncAction):
                    continue
                if action.kind != ACTION_CONFLICT:
                    continue
                combo = QComboBox(self._tree)
                for label, value in _CONFLICT_POLICY_CHOICES:
                    combo.addItem(label, value)
                # Pre-select the action's stored resolution.
                for idx in range(combo.count()):
                    if combo.itemData(idx) == action.conflict_resolution:
                        combo.setCurrentIndex(idx)
                        break
                combo.currentIndexChanged.connect(
                    lambda _i, c=combo, a=action: self._on_policy_changed(a, c)
                )
                index = self._model.indexFromItem(header.child(j, 3))
                self._tree.setIndexWidget(index, combo)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_policy_changed(self, action: SyncAction, combo: QComboBox) -> None:
        value = combo.currentData()
        if isinstance(value, str):
            action.conflict_resolution = value

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

    def approved_actions(self) -> list[SyncAction]:
        """Return the list of approved :class:`SyncAction` items.

        Identical actions are never in this list (their checkbox defaults
        to unchecked); the apply step doesn't need them.
        """
        out: list[SyncAction] = []
        for item in self._iter_action_items():
            if item.checkState() != Qt.CheckState.Checked:
                continue
            action = item.data(_ACTION_ROLE)
            if isinstance(action, SyncAction):
                out.append(action)
        return out

    def _approved_is_destructive(self, approved: list[SyncAction]) -> bool:
        for action in approved:
            if action.kind in {ACTION_DELETE_DEST, ACTION_DELETE_LOCAL}:
                return True
            if action.kind == ACTION_CONFLICT and action.conflict_resolution in {
                CONFLICT_RESOLUTION_LOCAL,
                CONFLICT_RESOLUTION_DEST,
                CONFLICT_RESOLUTION_NEWEST,
            }:
                return True
        return False

    def _on_apply_clicked(self) -> None:
        """Run the destructive-action double-confirm if required (§6.3)."""
        approved = self.approved_actions()
        if not approved:
            self.reject()
            return
        if self._approved_is_destructive(approved) and not self._confirm_destructive(
            approved
        ):
            return
        self._select_all_btn.setEnabled(False)
        self._deselect_all_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, len(approved))
        self._progress.setValue(0)
        self.actions_approved.emit(approved)

    def _confirm_destructive(self, approved: list[SyncAction]) -> bool:
        """Show both confirmation dialogs from spec §6.3. Returns True to proceed."""
        counts: defaultdict[str, int] = defaultdict(int)
        sizes: defaultdict[str, int] = defaultdict(int)
        for action in approved:
            counts[action.kind] += 1
            sizes[action.kind] += int(action.size_bytes or 0)
        n_add = counts.get(ACTION_COPY_TO_DEST, 0)
        n_delete = counts.get(ACTION_DELETE_DEST, 0) + counts.get(
            ACTION_DELETE_LOCAL, 0
        )
        n_overwrite = sum(
            1
            for a in approved
            if a.kind == ACTION_CONFLICT
            and a.conflict_resolution
            in {
                CONFLICT_RESOLUTION_LOCAL,
                CONFLICT_RESOLUTION_DEST,
                CONFLICT_RESOLUTION_NEWEST,
            }
        )
        bytes_add = sizes.get(ACTION_COPY_TO_DEST, 0)
        bytes_delete = sizes.get(ACTION_DELETE_DEST, 0) + sizes.get(
            ACTION_DELETE_LOCAL, 0
        )
        first_text = (
            "This sync will:\n"
            f"  • Add {n_add} files ({_format_bytes(bytes_add)})\n"
            f"  • DELETE {n_delete} files ({_format_bytes(bytes_delete)})\n"
            f"  • Overwrite {n_overwrite} files\n\n"
            "These changes cannot be undone automatically.\n"
            "The deleted files will NOT be moved to a trash folder."
        )
        first = QMessageBox.question(
            self,
            "Major changes to destination",
            first_text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if first != QMessageBox.StandardButton.Ok:
            return False
        second_text = (
            "Are you sure?\n\n"
            f"You're about to delete {n_delete} files from:\n"
            f"  {self._destination_label or 'the destination'}\n\n"
            "This is your last chance to cancel."
        )
        second = QMessageBox.question(
            self,
            "Apply the plan?",
            second_text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return second == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # Progress hooks driven by the caller while the worker runs.
    # ------------------------------------------------------------------

    def on_progress(self, current: int, total: int, label: str) -> None:
        if total > 0:
            self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._summary_label.setText(
            f"Applying {current} of {total}: {label}"
        )

    def on_finished(self, applied: int, skipped: int, failed: int) -> None:
        self._progress.setRange(0, max(1, self._progress.maximum()))
        self._progress.setValue(self._progress.maximum())
        icon = "✓" if failed == 0 else "✗"
        self._summary_label.setText(
            f"{icon} Done. Applied {applied}, skipped {skipped}, failed {failed}."
        )

    def on_failed(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._summary_label.setText(f"✗ {message}")


def _format_bytes(value: int | None) -> str:
    """Human-readable byte size — keeps the preview header short.

    Mirrors :func:`romulus.ui.game_table._format_size` (re-exported as
    ``_format_bytes`` in ``export_dialog``); reimplemented here so the sync
    dialog stays decoupled from the export-dialog module's import graph.
    """
    if not value:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    unit_idx = 0
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(size)} {units[unit_idx]}"
    return f"{size:.1f} {units[unit_idx]}"
