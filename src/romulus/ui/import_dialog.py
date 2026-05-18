"""Import-ROMs dialog — staging-folder picker + plan preview + apply.

The dialog runs two phases against the same widget hierarchy:

1. **Pre-analyse** — user picks a staging folder, ticks the
   heavy-identify / move-vs-copy options, and clicks **Analyse**.
2. **Post-analyse** — the populated :class:`ImportPlan` drives a tree of
   per-system actions; each row carries a resolution dropdown (skip /
   replace / keep-both) mirroring the sync-preview pattern from
   :mod:`romulus.ui.sync_preview`. Apply runs the plan via
   :class:`romulus.ui.workers.ImportApplyWorker`.

Workers live in :mod:`romulus.ui.workers`; this dialog only emits the
signals MainWindow needs to spawn them and exposes progress / completion
callbacks the workers can route into.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from romulus.core.importer import (
    ImportAction,
    ImportOptions,
    ImportPlan,
    ImportResolution,
)

logger = logging.getLogger(__name__)

# Map status -> (display label, group header).
_STATUS_LABELS: dict[str, str] = {
    "new": "New",
    "dupe_path": "Already in library",
    "dupe_filename": "Conflict (same name, different content)",
    "dupe_hash": "Already in library (different name)",
    "multi_rom_archive": "Multi-ROM archive",
}

#: Resolution dropdown entries shown for each action row. Same vocabulary
#: as :class:`romulus.core.importer.ImportResolution`; the dialog stamps
#: the chosen value back onto the action in place.
_RESOLUTION_CHOICES: tuple[tuple[str, ImportResolution], ...] = (
    ("Copy", "copy"),
    ("Move", "move"),
    ("Replace existing", "replace"),
    ("Keep both", "keep_both"),
    ("Skip", "skip"),
)


_ACTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class ImportDialog(QDialog):
    """Multi-phase dialog: pick staging → analyse → review → apply."""

    #: Emitted when the user clicks Analyse. Carries the chosen staging
    #: folder, the library root, and the resolved :class:`ImportOptions`.
    analyse_requested = Signal(str, str, object)

    #: Emitted when the user clicks Apply on the populated plan. Carries
    #: the :class:`ImportPlan` (with per-row resolutions already mutated
    #: in place via the resolution-dropdown delegate).
    apply_requested = Signal(object)

    def __init__(
        self,
        library_path: str,
        recent_paths: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import ROMs")
        self.setModal(True)
        self.resize(900, 720)
        self._library_path = library_path
        self._recent_paths = list(recent_paths or [])
        self._plan: ImportPlan | None = None

        layout = QVBoxLayout(self)

        # ---- Intro paragraph ------------------------------------------
        intro = QLabel(
            "Import ROMs from a staging folder (Downloads, USB stick, "
            "mounted archive) into the current library. ROMulus identifies "
            "each file, builds a plan, and lets you resolve duplicates "
            "before any files move.",
            self,
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #666; padding: 4px 0 8px 0;")
        layout.addWidget(intro)

        # ---- Library + staging picker ---------------------------------
        picker_form = QFormLayout()
        picker_form.setContentsMargins(0, 0, 0, 0)

        library_row = QLabel(library_path or "<no library configured>", self)
        library_row.setStyleSheet("color: #444;")
        picker_form.addRow("Library:", library_row)

        staging_row = QHBoxLayout()
        self._staging_combo = QComboBox(self)
        self._staging_combo.setEditable(True)
        self._staging_combo.setMinimumWidth(420)
        for recent in self._recent_paths:
            self._staging_combo.addItem(recent)
        if not self._recent_paths:
            self._staging_combo.setEditText("")
        staging_row.addWidget(self._staging_combo, stretch=1)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._on_browse_staging)
        staging_row.addWidget(browse)
        picker_form.addRow("Staging folder:", staging_row)
        layout.addLayout(picker_form)

        # ---- Options group --------------------------------------------
        options_group = QGroupBox("Import options", self)
        options_layout = QVBoxLayout(options_group)

        identify_note = QLabel(
            "ROMulus always hashes every staging file during identification "
            "(L1 filename + L2 header + L3 SHA-1 + DAT match). Hashing is "
            "what makes the duplicate detection accurate — you'll be warned "
            "before the run starts if the staging folder is large.",
            options_group,
        )
        identify_note.setWordWrap(True)
        identify_note.setStyleSheet("color: #666;")
        options_layout.addWidget(identify_note)

        move_row = QHBoxLayout()
        move_label = QLabel("Action:", options_group)
        self._copy_radio = QRadioButton("Copy (keep source intact)", options_group)
        self._copy_radio.setChecked(True)
        self._move_radio = QRadioButton("Move (delete source after copy)", options_group)
        move_row.addWidget(move_label)
        move_row.addWidget(self._copy_radio)
        move_row.addWidget(self._move_radio)
        move_row.addStretch(1)
        options_layout.addLayout(move_row)

        layout.addWidget(options_group)

        # ---- Analyse button ------------------------------------------
        analyse_row = QHBoxLayout()
        self._analyse_btn = QPushButton("Analyse staging folder", self)
        self._analyse_btn.clicked.connect(self._on_analyse_clicked)
        analyse_row.addWidget(self._analyse_btn)
        analyse_row.addStretch(1)
        layout.addLayout(analyse_row)

        # ---- Results summary + tree ----------------------------------
        self._summary_label = QLabel(
            "Click Analyse to scan the staging folder.", self
        )
        self._summary_label.setStyleSheet("font-weight: bold; padding: 4px 0;")
        layout.addWidget(self._summary_label)

        self._created_systems_label = QLabel("", self)
        self._created_systems_label.setStyleSheet("color: #0a5; padding: 0 0 4px 0;")
        self._created_systems_label.setWordWrap(True)
        self._created_systems_label.setVisible(False)
        layout.addWidget(self._created_systems_label)

        self._tree = QTreeView(self)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(
            ["Source", "Target", "Status", "Resolution"]
        )
        self._tree.setModel(self._model)
        self._tree.setColumnWidth(0, 280)
        self._tree.setColumnWidth(1, 320)
        self._tree.setColumnWidth(2, 200)
        layout.addWidget(self._tree, stretch=1)

        # Bulk-resolution row — pure UX sugar over the per-row dropdowns.
        bulk_row = QHBoxLayout()
        bulk_label = QLabel("Apply to all remaining conflicts:", self)
        self._bulk_combo = QComboBox(self)
        for label, value in _RESOLUTION_CHOICES:
            self._bulk_combo.addItem(label, value)
        # Default the bulk action to ``skip`` so a careless "Apply to all"
        # click never silently overwrites everything.
        self._bulk_combo.setCurrentIndex(
            next(
                i
                for i, (_lbl, val) in enumerate(_RESOLUTION_CHOICES)
                if val == "skip"
            )
        )
        self._bulk_btn = QPushButton("Apply", self)
        self._bulk_btn.clicked.connect(self._on_bulk_apply_clicked)
        bulk_row.addWidget(bulk_label)
        bulk_row.addWidget(self._bulk_combo)
        bulk_row.addWidget(self._bulk_btn)
        bulk_row.addStretch(1)
        self._bulk_row = bulk_row
        layout.addLayout(bulk_row)

        # ---- Progress bar (hidden until Apply) -----------------------
        self._progress = QProgressBar(self)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # ---- Action buttons -----------------------------------------
        button_row = QHBoxLayout()
        self._save_plan_btn = QPushButton("Save plan as JSON…", self)
        self._save_plan_btn.clicked.connect(self._on_save_plan_clicked)
        self._save_plan_btn.setEnabled(False)
        button_row.addWidget(self._save_plan_btn)
        button_row.addStretch(1)
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._apply_btn = self._button_box.button(
            QDialogButtonBox.StandardButton.Apply
        )
        self._apply_btn.setText("Apply import")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        self._cancel_btn = self._button_box.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.rejected.connect(self.reject)
        button_row.addWidget(self._button_box)
        layout.addLayout(button_row)

        # Initially hide the bulk-action row + tree empty-state nicety.
        self._set_results_visible(False)

    # ------------------------------------------------------------------
    # Public accessors (for MainWindow)
    # ------------------------------------------------------------------

    @property
    def plan(self) -> ImportPlan | None:
        """The currently-loaded :class:`ImportPlan` or None pre-analyse."""
        return self._plan

    @property
    def staging_path(self) -> str:
        """The text in the staging-folder picker (canonicalized)."""
        return self._staging_combo.currentText().strip()

    # ------------------------------------------------------------------
    # Pre-analyse phase
    # ------------------------------------------------------------------

    def _on_browse_staging(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Pick staging folder", self.staging_path or str(Path.home())
        )
        if directory:
            self._staging_combo.setEditText(directory)

    def _on_analyse_clicked(self) -> None:
        staging = self.staging_path
        if not staging:
            QMessageBox.warning(
                self,
                "Pick a staging folder",
                "Choose the folder ROMulus should import from.",
            )
            return
        if not self._library_path:
            QMessageBox.warning(
                self,
                "No library configured",
                "Set a library folder under File → Open Library first.",
            )
            return
        if not Path(staging).is_dir():
            QMessageBox.warning(
                self,
                "Staging folder not found",
                f"The folder does not exist:\n{staging}",
            )
            return

        # Pre-flight size scan — heavy identification will hash every file,
        # so we warn up front when the run is going to be slow. The cheap
        # walk below counts files and sums sizes; running it on the main
        # thread is fine because file metadata (st_size) is fast even on
        # network shares — it's the file content read inside hash_rom that
        # blows out the latency on slow links.
        estimate = _estimate_staging_size(Path(staging))
        if not _confirm_estimate(self, estimate):
            return

        options = ImportOptions(
            default_resolution="move" if self._move_radio.isChecked() else "copy",
            heavy_identify=True,
        )
        self._analyse_btn.setEnabled(False)
        self._summary_label.setText(
            f"Analysing {staging} (hashing {estimate.file_count} file(s))…"
        )
        self.analyse_requested.emit(staging, self._library_path, options)

    # ------------------------------------------------------------------
    # Hooks for workers — MainWindow connects these to ImportAnalyseWorker
    # ------------------------------------------------------------------

    def on_analyse_progress(self, current: int, total: int, label: str) -> None:
        if total > 0:
            self._summary_label.setText(
                f"Analysing {current}/{total}: {label}"
            )

    def on_analyse_finished(self, plan: object) -> None:
        if not isinstance(plan, ImportPlan):
            self._analyse_btn.setEnabled(True)
            return
        self._plan = plan
        self._populate_tree(plan)
        self._update_summary(plan)
        self._set_results_visible(True)
        self._save_plan_btn.setEnabled(True)
        self._apply_btn.setEnabled(bool(plan.actions))
        self._analyse_btn.setEnabled(True)

    def on_analyse_failed(self, message: str) -> None:
        self._summary_label.setText(message)
        self._analyse_btn.setEnabled(True)

    def on_apply_progress(self, current: int, total: int, label: str) -> None:
        if total > 0:
            self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._summary_label.setText(
            f"Importing {current}/{total}: {label}"
        )

    def on_apply_finished(
        self,
        imported: int,
        skipped: int,
        replaced: int,
        kept_both: int,
        bytes_imported: int,
        _systems: list,
        errors: list,
    ) -> None:
        self._progress.setRange(0, max(1, self._progress.maximum()))
        self._progress.setValue(self._progress.maximum())
        size_str = _format_bytes(int(bytes_imported))
        icon = "✓" if not errors else "✗"
        self._summary_label.setText(
            f"{icon} Imported {imported} file(s) ({size_str}), "
            f"skipped {skipped}, replaced {replaced}, kept-both "
            f"{kept_both}, errors {len(errors)}."
        )
        self._enter_done_state()

    def on_apply_failed(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._summary_label.setText(f"✗ {message}")
        self._enter_done_state()

    # ------------------------------------------------------------------
    # Tree population
    # ------------------------------------------------------------------

    def _set_results_visible(self, visible: bool) -> None:
        self._tree.setVisible(visible)
        # Hide the bulk-resolution row when there are no rows.
        for i in range(self._bulk_row.count()):
            widget = self._bulk_row.itemAt(i).widget()
            if widget is not None:
                widget.setVisible(visible)

    def _populate_tree(self, plan: ImportPlan) -> None:
        self._model.removeRows(0, self._model.rowCount())
        # Group by system_id (None → "Unsorted").
        by_system: defaultdict[str, list[ImportAction]] = defaultdict(list)
        for action in plan.actions:
            key = action.system_id or "_unsorted"
            by_system[key].append(action)
        root = self._model.invisibleRootItem()
        for system_id in sorted(by_system.keys()):
            actions = by_system[system_id]
            badge = " (new)" if system_id in plan.created_systems else ""
            header = QStandardItem(
                f"{system_id}{badge} — {len(actions)} file(s)"
            )
            header.setEditable(False)
            header.setSelectable(False)
            for action in actions:
                source_item = QStandardItem(
                    str(action.source_path.relative_to(plan.staging_root))
                    if _is_under(action.source_path, plan.staging_root)
                    else str(action.source_path)
                )
                source_item.setEditable(False)
                source_item.setData(action, _ACTION_ROLE)
                target_item = QStandardItem(str(action.target_path))
                target_item.setEditable(False)
                status_text = _STATUS_LABELS.get(action.status, action.status)
                if action.reason:
                    status_text = f"{status_text} — {action.reason}"
                status_item = QStandardItem(status_text)
                status_item.setEditable(False)
                resolution_placeholder = QStandardItem("")
                resolution_placeholder.setEditable(False)
                header.appendRow(
                    [source_item, target_item, status_item, resolution_placeholder]
                )
            root.appendRow(header)
        self._tree.expandAll()
        self._install_resolution_widgets()

    def _install_resolution_widgets(self) -> None:
        """Attach a QComboBox to the Resolution column for every leaf row."""
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            header = root.child(i, 0)
            if header is None:
                continue
            for j in range(header.rowCount()):
                source_item = header.child(j, 0)
                if source_item is None:
                    continue
                action = source_item.data(_ACTION_ROLE)
                if not isinstance(action, ImportAction):
                    continue
                combo = QComboBox(self._tree)
                for label, value in _RESOLUTION_CHOICES:
                    combo.addItem(label, value)
                for idx in range(combo.count()):
                    if combo.itemData(idx) == action.resolution:
                        combo.setCurrentIndex(idx)
                        break
                combo.currentIndexChanged.connect(
                    lambda _i, c=combo, a=action: self._on_resolution_changed(a, c)
                )
                index = self._model.indexFromItem(header.child(j, 3))
                self._tree.setIndexWidget(index, combo)

    def _on_resolution_changed(
        self, action: ImportAction, combo: QComboBox
    ) -> None:
        value = combo.currentData()
        if isinstance(value, str):
            action.resolution = value  # type: ignore[assignment]

    def _on_bulk_apply_clicked(self) -> None:
        """Apply the bulk-row resolution to every conflict action.

        ``dupe_filename`` is the only status with a real conflict-resolution
        question; ``new`` rows already default to copy/move and ``dupe_path``
        / ``dupe_hash`` default to skip and we don't want to surprise the
        user by sweeping those into a bulk replace.
        """
        if self._plan is None:
            return
        value = self._bulk_combo.currentData()
        if not isinstance(value, str):
            return
        for action in self._plan.actions:
            if action.status == "dupe_filename":
                action.resolution = value  # type: ignore[assignment]
        # Rebuild the tree so the dropdowns reflect the new selection.
        self._populate_tree(self._plan)

    # ------------------------------------------------------------------
    # Apply phase
    # ------------------------------------------------------------------

    def _on_apply_clicked(self) -> None:
        if self._plan is None:
            return
        # Destructive double-confirm if any resolution will overwrite a file
        # on disk — replace + (move replacing an existing file) qualify.
        has_replace = any(
            a.resolution == "replace" for a in self._plan.actions
        )
        if has_replace:
            confirm = QMessageBox.question(
                self,
                "Overwrite existing files?",
                "Some actions are set to REPLACE the existing file on "
                "disk. This cannot be undone — the replaced file is not "
                "moved to a trash folder.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        self._apply_btn.setEnabled(False)
        self._analyse_btn.setEnabled(False)
        self._save_plan_btn.setEnabled(False)
        self._bulk_btn.setEnabled(False)
        actions_to_run = sum(
            1 for a in self._plan.actions if a.resolution != "skip"
        )
        self._progress.setVisible(True)
        self._progress.setRange(0, max(1, actions_to_run or len(self._plan.actions)))
        self._progress.setValue(0)
        self.apply_requested.emit(self._plan)

    def _on_save_plan_clicked(self) -> None:
        if self._plan is None:
            return
        default = str(
            Path.home() / f"romulus-import-plan-{self._plan.staging_root.name}.json"
        )
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save import plan", default, "JSON files (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self._plan.to_json(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(
                self, "Save plan", f"Could not save plan:\n{exc}"
            )
            return
        QMessageBox.information(
            self, "Save plan", f"Plan saved to {path}."
        )

    # ------------------------------------------------------------------
    # Summary text
    # ------------------------------------------------------------------

    def _update_summary(self, plan: ImportPlan) -> None:
        if not plan.actions:
            self._summary_label.setText(
                "Staging folder analysed: no importable files found."
            )
            self._created_systems_label.setVisible(False)
            return
        counts: dict[str, int] = defaultdict(int)
        for action in plan.actions:
            counts[action.status] += 1
        total = len(plan.actions)
        new = counts.get("new", 0)
        dupe_path = counts.get("dupe_path", 0)
        dupe_filename = counts.get("dupe_filename", 0)
        dupe_hash = counts.get("dupe_hash", 0)
        multi = counts.get("multi_rom_archive", 0)
        parts = [
            f"{total} file(s)",
            f"{new} new",
            f"{dupe_path} already enrolled",
            f"{dupe_filename} conflicts",
        ]
        if dupe_hash:
            parts.append(f"{dupe_hash} hash-dupes")
        if multi:
            parts.append(f"{multi} multi-rom archives")
        parts.append(f"{_format_bytes(plan.total_bytes)} to copy")
        self._summary_label.setText("  •  ".join(parts))
        if plan.created_systems:
            self._created_systems_label.setText(
                "New system folders will be created: "
                + ", ".join(sorted(plan.created_systems))
            )
            self._created_systems_label.setVisible(True)
        else:
            self._created_systems_label.setVisible(False)

    # ------------------------------------------------------------------
    # Completion state
    # ------------------------------------------------------------------

    def _enter_done_state(self) -> None:
        """Swap Apply/Cancel for a single Close button after apply finishes."""
        if getattr(self, "_done_state", False):
            return
        self._done_state = True
        if self._apply_btn is not None:
            self._apply_btn.setVisible(False)
        if self._cancel_btn is not None:
            self._cancel_btn.setText("Close")
            import contextlib

            with contextlib.suppress(RuntimeError, TypeError):
                self._cancel_btn.clicked.disconnect()
            self._cancel_btn.clicked.connect(self.accept)


# ---------------------------------------------------------------------------
# Pre-flight size estimate (used to warn before kicking off heavy identify)
# ---------------------------------------------------------------------------


#: Threshold for the "large total size" warning. ~1 GiB total typically
#: means the staging folder contains CD-based ISOs that hash slowly even
#: on local disks.
_TOTAL_BYTES_WARN_THRESHOLD = 1 * 1024**3
#: Threshold for the "lots of files" warning. Below this the run is fast
#: enough that warning would just be noise.
_FILE_COUNT_WARN_THRESHOLD = 100
#: Threshold for the "contains huge files" warning. CD ISOs are typically
#: 200–700 MiB; one file past this size signals the run will be slow.
_LARGE_FILE_THRESHOLD = 100 * 1024**2


@dataclass(slots=True)
class _SizeEstimate:
    """Cheap file-count + byte-total summary used by the pre-flight prompt."""

    file_count: int
    total_bytes: int
    largest_file_bytes: int


def _estimate_staging_size(staging_root: Path) -> _SizeEstimate:
    """Walk the staging folder collecting cheap metadata only.

    Reads ``stat()`` per file (no content) so this is fast even on slow
    network shares. Failures to stat individual files are swallowed —
    they will surface again during the analyse pass with a real error.
    """
    file_count = 0
    total_bytes = 0
    largest = 0
    for root, _dirs, files in os.walk(staging_root, followlinks=False):
        for filename in files:
            file_count += 1
            try:
                size = (Path(root) / filename).stat().st_size
            except OSError:
                continue
            total_bytes += size
            if size > largest:
                largest = size
    return _SizeEstimate(
        file_count=file_count,
        total_bytes=total_bytes,
        largest_file_bytes=largest,
    )


def _confirm_estimate(parent: QWidget, estimate: _SizeEstimate) -> bool:
    """Show a duration warning if the estimate crosses any threshold.

    Returns True when the run should proceed (either no warning was
    needed, or the user accepted). Returns False when the user cancelled.
    """
    triggers: list[str] = []
    if estimate.file_count > _FILE_COUNT_WARN_THRESHOLD:
        triggers.append(f"{estimate.file_count:,} files")
    if estimate.total_bytes > _TOTAL_BYTES_WARN_THRESHOLD:
        triggers.append(f"{_format_bytes(estimate.total_bytes)} total")
    if estimate.largest_file_bytes > _LARGE_FILE_THRESHOLD:
        triggers.append(
            f"largest file {_format_bytes(estimate.largest_file_bytes)} "
            "(CD-image-sized)"
        )
    if not triggers:
        return True
    msg = (
        "Heavy identification will hash every file before building the "
        "import plan. The staging folder contains:\n\n  • "
        + "\n  • ".join(triggers)
        + "\n\nThis can take several minutes (or longer over a network "
        "share). Continue?"
    )
    answer = QMessageBox.question(
        parent,
        "Hashing may take a while",
        msg,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    return answer == QMessageBox.StandardButton.Yes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _format_bytes(value: int | None) -> str:
    """Human-readable byte size matching sync_preview's formatting."""
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


# ---------------------------------------------------------------------------
# Recent-paths persistence helpers — used by MainWindow.
# ---------------------------------------------------------------------------


_RECENT_CONFIG_KEY = "import_recent_paths"
_RECENT_LIMIT = 5


def load_recent_staging_paths(get_config_fn, conn) -> list[str]:
    """Read the recent-staging-folder list from config.

    ``get_config_fn`` is a callable matching :func:`romulus.db.get_config`
    — passed in rather than imported so this helper can be unit-tested
    against a fake config store without spinning up the full app harness.
    """
    raw = get_config_fn(conn, _RECENT_CONFIG_KEY) or "[]"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, str)][:_RECENT_LIMIT]


def remember_staging_path(set_config_fn, get_config_fn, conn, path: str) -> None:
    """Move ``path`` to the front of the recent-staging list (capped at 5)."""
    if not path:
        return
    current = load_recent_staging_paths(get_config_fn, conn)
    # Bring `path` to the front, drop earlier copies.
    updated = [path] + [p for p in current if p != path]
    updated = updated[:_RECENT_LIMIT]
    set_config_fn(conn, _RECENT_CONFIG_KEY, json.dumps(updated))
