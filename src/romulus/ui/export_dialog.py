"""Export dialog — profile selector, path chooser, filters, progress.

The dialog drives the export pipeline end-to-end without blocking the UI
thread. The Preview button runs :func:`preview_export` synchronously (no
filesystem writes, the work is cheap) and shows the file count, total size,
and per-folder tree. The Export button hands off to :class:`ExportWorker`
which streams ``progress(current, total, filename)`` ticks back into the
dialog's progress bar.

Filters: system checkboxes, an optional collection filter, region check-
boxes, and the standard sidecar toggles (artwork, gamelist.xml, .m3u). All
filters are passed to the preview AND the export so the two stay consistent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from romulus.core.exporter import (
    ExportFilters,
    ExportOptions,
    preview_export,
)
from romulus.db import queries as q
from romulus.models.profile import DestinationProfile
from romulus.models.system import SYSTEM_REGISTRY

#: Canonical ordered list of region tags the filter UI exposes. ``"Other"``
#: is a catch-all that matches games whose region is NULL or anything not
#: explicitly listed.
_REGION_OPTIONS: tuple[str, ...] = ("USA", "Europe", "Japan", "World", "Other")


# ``_format_bytes`` used to be a near-duplicate of ``_format_size`` in
# ``ui/game_table.py``. Re-exported here so existing call sites and any
# future readers see one canonical implementation.
from romulus.ui.game_table import _format_size as _format_bytes  # noqa: E402


class ExportDialog(QDialog):
    """Profile selector + filters + preview + export progress."""

    #: Emitted when the user clicks Export with valid inputs. MainWindow
    #: spawns the :class:`ExportWorker` in response. Carries
    #: ``(profile, target_path, filters, options)``.
    export_requested = Signal(object, str, object, object)

    def __init__(
        self,
        conn: sqlite3.Connection,
        profiles: dict[str, DestinationProfile],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Collection")
        self.setModal(True)
        self.resize(720, 720)
        self._conn = conn
        self._profiles = profiles

        layout = QVBoxLayout(self)

        # ---- Profile + target path ------------------------------------
        form = QFormLayout()
        self._profile_combo = QComboBox(self)
        for profile_id, profile in sorted(profiles.items()):
            self._profile_combo.addItem(profile.name, profile_id)
        form.addRow("Destination profile:", self._profile_combo)

        path_row = QHBoxLayout()
        self._target_edit = QLineEdit(self)
        self._target_edit.setPlaceholderText("Select an export folder...")
        path_row.addWidget(self._target_edit)
        browse_btn = QPushButton("Browse...", self)
        browse_btn.clicked.connect(self._on_browse_target)
        path_row.addWidget(browse_btn)
        form.addRow("Target path:", path_row)
        layout.addLayout(form)

        # ---- System filter --------------------------------------------
        systems_group = QGroupBox("Systems", self)
        systems_layout = QVBoxLayout(systems_group)
        self._systems_list = QListWidget(systems_group)
        for sys_def in SYSTEM_REGISTRY:
            item = QListWidgetItem(f"{sys_def.short_name} ({sys_def.id})")
            item.setData(Qt.ItemDataRole.UserRole, sys_def.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self._systems_list.addItem(item)
        systems_layout.addWidget(self._systems_list)
        layout.addWidget(systems_group)

        # ---- Collection + region filters ------------------------------
        filter_form = QFormLayout()
        self._collection_combo = QComboBox(self)
        self._collection_combo.addItem("All games", None)
        for row in q.get_collections(conn):
            self._collection_combo.addItem(
                f"{row['name']} ({row['game_count']})", int(row["id"])
            )
        filter_form.addRow("Collection:", self._collection_combo)

        region_row = QHBoxLayout()
        self._region_checks: dict[str, QCheckBox] = {}
        for region in _REGION_OPTIONS:
            cb = QCheckBox(region, self)
            cb.setChecked(True)
            self._region_checks[region] = cb
            region_row.addWidget(cb)
        region_row.addStretch(1)
        region_widget = QWidget(self)
        region_widget.setLayout(region_row)
        filter_form.addRow("Regions:", region_widget)
        layout.addLayout(filter_form)

        # ---- Sidecar option toggles -----------------------------------
        options_group = QGroupBox("Options", self)
        options_layout = QVBoxLayout(options_group)
        self._include_artwork_cb = QCheckBox("Include artwork", options_group)
        self._include_artwork_cb.setChecked(False)
        options_layout.addWidget(self._include_artwork_cb)
        self._generate_gamelist_cb = QCheckBox(
            "Generate gamelist.xml / .lpl", options_group
        )
        self._generate_gamelist_cb.setChecked(True)
        options_layout.addWidget(self._generate_gamelist_cb)
        self._generate_m3u_cb = QCheckBox(
            "Generate .m3u for multi-disc games", options_group
        )
        self._generate_m3u_cb.setChecked(True)
        options_layout.addWidget(self._generate_m3u_cb)
        layout.addWidget(options_group)

        # ---- Preview output -------------------------------------------
        self._preview_text = QTextEdit(self)
        self._preview_text.setReadOnly(True)
        self._preview_text.setPlaceholderText(
            "Click Preview to see what will be exported."
        )
        layout.addWidget(self._preview_text, stretch=1)

        # ---- Progress bar (hidden until Export starts) ---------------
        self._progress = QProgressBar(self)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # ---- Status label ---------------------------------------------
        self._status_label = QLabel("", self)
        layout.addWidget(self._status_label)

        # ---- Action buttons ------------------------------------------
        button_box = QDialogButtonBox(self)
        self._preview_btn = button_box.addButton(
            "Preview", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._preview_btn.clicked.connect(self._on_preview)
        self._export_btn = button_box.addButton(
            "Export", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._export_btn.clicked.connect(self._on_export_clicked)
        cancel_btn = button_box.addButton(
            "Close", QDialogButtonBox.ButtonRole.RejectRole
        )
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------
    # Filter / option accessors
    # ------------------------------------------------------------------

    def selected_profile(self) -> DestinationProfile | None:
        """The currently-selected destination profile, or None if none loaded."""
        profile_id = self._profile_combo.currentData()
        if profile_id is None:
            return None
        return self._profiles.get(str(profile_id))

    def selected_target_path(self) -> str:
        return self._target_edit.text().strip()

    def selected_systems(self) -> list[str]:
        """System ids of every checked entry in the systems list."""
        out: list[str] = []
        for i in range(self._systems_list.count()):
            item = self._systems_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return out

    def selected_regions(self) -> list[str]:
        return [
            region
            for region, cb in self._region_checks.items()
            if cb.isChecked()
        ]

    def selected_collection_id(self) -> int | None:
        data = self._collection_combo.currentData()
        return int(data) if isinstance(data, int) else None

    def build_filters(self) -> ExportFilters:
        systems = self.selected_systems()
        regions = self.selected_regions()
        return ExportFilters(
            systems=systems or None,
            regions=regions or None,
            collection_id=self.selected_collection_id(),
        )

    def build_options(self) -> ExportOptions:
        return ExportOptions(
            include_artwork=self._include_artwork_cb.isChecked(),
            generate_gamelist=self._generate_gamelist_cb.isChecked(),
            generate_m3u=self._generate_m3u_cb.isChecked(),
        )

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_browse_target(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose export folder", str(Path.home())
        )
        if chosen:
            self._target_edit.setText(chosen)

    def _on_preview(self) -> None:
        profile = self.selected_profile()
        target = self.selected_target_path()
        if profile is None or not target:
            self._preview_text.setPlainText(
                "Select a destination profile and target path first."
            )
            return
        filters = self.build_filters()
        preview = preview_export(self._conn, profile, target, filters)
        lines: list[str] = []
        lines.append(
            f"Exporting {preview.file_count} ROM(s) "
            f"({_format_bytes(preview.total_size_bytes)}) "
            f"across {len(preview.by_system)} system(s)."
        )
        if preview.unsupported_systems:
            lines.append(
                "Skipping unsupported system(s): "
                + ", ".join(preview.unsupported_systems)
            )
        lines.append("")
        for folder, filenames in sorted(preview.folder_tree.items()):
            lines.append(f"{folder}/  ({len(filenames)} file(s))")
            # Show only the first 5 filenames per folder to keep the panel readable.
            for name in filenames[:5]:
                lines.append(f"    {name}")
            if len(filenames) > 5:
                lines.append(f"    ... and {len(filenames) - 5} more")
        self._preview_text.setPlainText("\n".join(lines))

    def _on_export_clicked(self) -> None:
        """Validate inputs and emit :pyattr:`export_requested`.

        The dialog stays open so the user can watch the progress bar; the
        MainWindow handler is responsible for spawning the
        :class:`ExportWorker`. Buttons are disabled while the export runs so
        the user can't double-click into a second worker.
        """
        profile = self.selected_profile()
        target = self.selected_target_path()
        if profile is None or not target:
            self._status_label.setText(
                "Select a destination profile and target path first."
            )
            return
        self._preview_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._status_label.setText("Starting export...")
        self.export_requested.emit(
            profile, target, self.build_filters(), self.build_options()
        )

    # ------------------------------------------------------------------
    # Progress hooks driven by the caller while the worker runs
    # ------------------------------------------------------------------

    def on_progress(self, current: int, total: int, filename: str) -> None:
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
        self._status_label.setText(f"Exporting {current} of {total}: {filename}")

    def on_finished(
        self,
        files_copied: int,
        files_skipped: int,
        bytes_copied: int,
        systems: list[str],
        errors: list[str],
    ) -> None:
        """Slot — stop the spinner, show the final summary."""
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        icon = "✓" if not errors else "✗"
        summary = (
            f"{icon} Exported {files_copied} file(s) "
            f"({_format_bytes(bytes_copied)}) "
            f"across {len(systems)} system(s)."
        )
        if files_skipped:
            summary += f"  Skipped {files_skipped}."
        if errors:
            summary += f"  {len(errors)} error(s)."
        self._status_label.setText(summary)

    def on_failed(self, message: str) -> None:
        """Slot — stop the spinner, show an error message."""
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._status_label.setText(f"✗ {message}")
