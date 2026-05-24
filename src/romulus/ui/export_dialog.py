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
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
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
from romulus.core.sync import ConflictPolicy, SyncMode
from romulus.db import queries as q
from romulus.models.profile import DestinationProfile
from romulus.models.system import SYSTEM_REGISTRY

#: Canonical ordered list of region tags the filter UI exposes. ``"Other"``
#: is a catch-all that matches games whose region is NULL or anything not
#: explicitly listed.
_REGION_OPTIONS: tuple[str, ...] = ("USA", "Europe", "Japan", "World", "Other")

#: Mode dropdown entries — ordered to match the spec's mock-up. Each tuple
#: is ``(label, mode_id)``; the mode_id values match :data:`romulus.core.
#: sync.SyncMode`.
_SYNC_MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("Push — merge", "push_merge"),
    ("Push — mirror", "push_mirror"),
    ("Push — fresh wipe", "push_wipe"),
    ("Pull — merge", "pull"),
    ("Two-way", "two_way"),
)


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

    #: Emitted when the user clicks "Scan destination" — MainWindow spawns
    #: the :class:`DestInventoryWorker`. Carries ``(profile, target_path,
    #: mode, deep_verify)`` plus the selected destination id. The id is
    #: ``-1`` only as a fallback when no destinations are saved yet; in that
    #: case MainWindow upgrades it via
    #: :func:`romulus.db.queries.ensure_sync_destination_by_path` before
    #: spawning the worker so every ``dest_inventory`` row has a valid FK.
    sync_scan_requested = Signal(object, str, str, bool, int)

    def __init__(
        self,
        conn: sqlite3.Connection,
        profiles: dict[str, DestinationProfile],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export / Sync Collection")
        self.setModal(True)
        self.resize(720, 760)
        self._conn = conn
        self._profiles = profiles

        layout = QVBoxLayout(self)

        # ---- Mode selector + saved destinations ----------------------
        form = QFormLayout()
        self._mode_combo = QComboBox(self)
        for label, value in _SYNC_MODE_CHOICES:
            self._mode_combo.addItem(label, value)
        form.addRow("Mode:", self._mode_combo)

        # Single Destination row replaces the previous trio of
        # Destination + Destination profile + Target path. Picking a saved
        # destination implies its path AND profile — there's no separate
        # "target" the user has to fill in. The "+ Add..." button opens a
        # short three-step wizard (folder picker → profile picker → name)
        # to register a new destination.
        dest_row = QHBoxLayout()
        self._destination_combo = QComboBox(self)
        self._destination_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._populate_destination_combo()
        self._destination_combo.currentIndexChanged.connect(
            self._on_destination_changed
        )
        dest_row.addWidget(self._destination_combo, stretch=1)
        self._save_dest_btn = QPushButton("+ Add...", self)
        self._save_dest_btn.setToolTip(
            "Register a new named destination — folder, profile, and label."
        )
        self._save_dest_btn.clicked.connect(self._on_save_destination)
        dest_row.addWidget(self._save_dest_btn)
        dest_widget = QWidget(self)
        dest_widget.setLayout(dest_row)
        form.addRow("Destination:", dest_widget)
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
        self._include_roms_cb = QCheckBox("Include ROMs", options_group)
        self._include_roms_cb.setChecked(True)
        self._include_roms_cb.setToolTip(
            "Uncheck to skip ROM copies and only refresh artwork + "
            "gamelist.xml on the destination. Use this after a Find "
            "Covers / Enrich Metadata run to push the fresh sidecars "
            "without re-copying gigabytes of already-synced ROMs."
        )
        self._include_roms_cb.stateChanged.connect(
            self._on_include_roms_changed
        )
        options_layout.addWidget(self._include_roms_cb)
        self._include_artwork_cb = QCheckBox("Include artwork", options_group)
        self._include_artwork_cb.setChecked(True)
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
        self._deep_verify_cb = QCheckBox(
            "Deep verify (slow — recomputes SHA-1 for every dest file)",
            options_group,
        )
        self._deep_verify_cb.setChecked(False)
        options_layout.addWidget(self._deep_verify_cb)
        self._distinct_content_cb = QCheckBox(
            "Export distinct content only (skip byte-identical duplicates)",
            options_group,
        )
        self._distinct_content_cb.setChecked(False)
        self._distinct_content_cb.setToolTip(
            "When checked, for each cluster of ROMs that share the same SHA-1 "
            "only the highest-ranked one is exported. Ranking: "
            "dat_verified > canonical extension (.sfc over .smc, etc.) > "
            "shorter filename > lower internal id.  "
            "ROMs with no SHA-1 (Quick-Scan-only) always pass through.  "
            "Useful if you copied your library twice and want a compact gamelist "
            "on the device without duplicate entries."
        )
        self._distinct_content_cb.stateChanged.connect(self._on_options_changed)
        options_layout.addWidget(self._distinct_content_cb)
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
        self._scan_dest_btn = button_box.addButton(
            "Scan destination", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._scan_dest_btn.clicked.connect(self._on_scan_destination)
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
        """Profile attached to the selected destination, or None if no
        destination is picked (or the profile YAML has been deleted)."""
        dest_id = self.selected_destination_id()
        if dest_id <= 0:
            return None
        row = q.get_sync_destination(self._conn, dest_id)
        if row is None:
            return None
        return self._profiles.get(str(row["profile_id"]))

    def selected_target_path(self) -> str:
        """Path of the selected destination, or '' if none picked."""
        dest_id = self.selected_destination_id()
        if dest_id <= 0:
            return ""
        row = q.get_sync_destination(self._conn, dest_id)
        return str(row["target_path"]) if row is not None else ""

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
            include_roms=self._include_roms_cb.isChecked(),
            include_artwork=self._include_artwork_cb.isChecked(),
            generate_gamelist=self._generate_gamelist_cb.isChecked(),
            generate_m3u=self._generate_m3u_cb.isChecked(),
            distinct_content_only=self._distinct_content_cb.isChecked(),
        )

    def selected_sync_mode(self) -> SyncMode:
        """The currently-selected sync mode from the Mode dropdown."""
        value = self._mode_combo.currentData()
        return str(value) if value else "push_merge"  # type: ignore[return-value]

    def selected_destination_id(self) -> int:
        """Saved-destination id, or ``-1`` if no destinations are saved yet.

        When ``-1`` is emitted MainWindow upgrades it via
        :func:`romulus.db.queries.ensure_sync_destination_by_path` before
        spawning the worker so every ``dest_inventory`` row has a valid FK.
        """
        data = self._destination_combo.currentData()
        return int(data) if isinstance(data, int) else -1

    def deep_verify_enabled(self) -> bool:
        return self._deep_verify_cb.isChecked()

    def selected_conflict_policy(self) -> ConflictPolicy:
        """Two-way's policy lives on the preview dialog. Default to skip."""
        return "skip"

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_destination_changed(self, _index: int) -> None:
        """Hook for downstream UI that needs to react to destination changes.

        Currently a no-op — path and profile are derived from the selected
        row at action time via :meth:`selected_target_path` /
        :meth:`selected_profile`. Kept as a hook for future per-destination
        UI hints (last-synced timestamp, etc.).
        """
        return

    def _populate_destination_combo(
        self, select_dest_id: int | None = None
    ) -> None:
        """Reload the destination dropdown from the DB.

        Each entry's label includes the destination's profile name so the
        user can tell at a glance which device a given target is for —
        ``Anbernic USB → E:/Roms  (Anbernic RGLauncher)``. With zero saved
        destinations we show a placeholder and the "+" button is the
        only action.
        """
        self._destination_combo.blockSignals(True)
        self._destination_combo.clear()
        rows = list(q.get_sync_destinations(self._conn))
        if not rows:
            self._destination_combo.addItem(
                "(no destinations — click + Add to create one)", -1
            )
        else:
            for row in rows:
                profile = self._profiles.get(str(row["profile_id"]))
                pname = profile.name if profile else str(row["profile_id"])
                label = f"{row['name']} → {row['target_path']}  ({pname})"
                self._destination_combo.addItem(label, int(row["id"]))
                if (
                    select_dest_id is not None
                    and int(row["id"]) == select_dest_id
                ):
                    self._destination_combo.setCurrentIndex(
                        self._destination_combo.count() - 1
                    )
        self._destination_combo.blockSignals(False)

    def _on_save_destination(self) -> None:
        """Three-step wizard: folder picker → profile picker → name.

        Replaces the previous workflow where the user had to fill in
        Target path + Destination profile fields BEFORE clicking "+".
        Now "+" is fully self-contained: pick what you want, name it,
        done. New destination is auto-selected on return.
        """
        if not self._profiles:
            QMessageBox.warning(
                self,
                "No profiles loaded",
                "ROMulus couldn't find any destination profile YAML files. "
                "Check your install's ``profiles/`` folder.",
            )
            return
        target = QFileDialog.getExistingDirectory(
            self, "Choose destination folder", str(Path.home())
        )
        if not target:
            return
        profile_items: list[tuple[str, str]] = [
            (p.name, pid) for pid, p in sorted(self._profiles.items())
        ]
        profile_names = [name for name, _ in profile_items]
        profile_name, ok = QInputDialog.getItem(
            self,
            "Choose destination profile",
            "Which device / launcher will read this folder?",
            profile_names,
            current=0,
            editable=False,
        )
        if not ok:
            return
        profile_id = next(
            (pid for name, pid in profile_items if name == profile_name),
            None,
        )
        if profile_id is None:
            return
        default_name = Path(target).name or target
        name, ok = QInputDialog.getText(
            self,
            "Name this destination",
            "Label (shown in the dropdown):",
            text=default_name,
        )
        if not ok or not name.strip():
            return
        try:
            new_id = q.insert_sync_destination(
                self._conn,
                {
                    "name": name.strip(),
                    "target_path": target,
                    "profile_id": profile_id,
                },
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            QMessageBox.warning(
                self,
                "Name already in use",
                f"A destination called '{name.strip()}' already exists. "
                "Pick a different name.",
            )
            return
        self._populate_destination_combo(select_dest_id=new_id)

    def _on_options_changed(self, _state: int) -> None:
        """Slot — refresh the preview whenever a non-structural option changes.

        Called when the distinct-content toggle (and any future checkbox that
        doesn't require a Scan destination first) flips. If the preview pane
        already has content we re-run it so the user immediately sees the
        projected skip count change.
        """
        if self._preview_text.toPlainText().strip():
            self._on_preview()

    def _on_include_roms_changed(self, _state: int) -> None:
        """Disable Scan destination + adjust UX when the user opts out of ROMs.

        ``Scan destination`` only earns its keep when the export is
        going to copy or delete ROM bytes — it walks the entire dest
        filesystem (potentially tens of thousands of files), runs the
        4-tier identity matcher, and produces a sync plan whose every
        row would be ``ACTION_IDENTICAL`` in artwork-only mode.
        Applying such a plan wouldn't refresh sidecars anyway (identical
        actions don't touch artwork). So when ``Include ROMs`` is off,
        the button is disabled with an explanatory tooltip and the
        user is funneled toward Export, which actually runs the
        sidecar refresh.
        """
        include_roms = self._include_roms_cb.isChecked()
        self._scan_dest_btn.setEnabled(include_roms)
        if include_roms:
            self._scan_dest_btn.setToolTip(
                "Walk the destination and diff against the local "
                "library — required for sync modes that may delete or "
                "overwrite dest files."
            )
        else:
            self._scan_dest_btn.setToolTip(
                "Disabled in artwork-only mode. Click Export to "
                "refresh artwork + gamelist without scanning the "
                "destination."
            )
        # Refresh the Preview pane if it's already populated so its text
        # reflects the new mode without the user having to re-click.
        if self._preview_text.toPlainText().strip():
            self._on_preview()

    def _on_scan_destination(self) -> None:
        """Emit :pyattr:`sync_scan_requested` for MainWindow to spawn the worker."""
        profile = self.selected_profile()
        target = self.selected_target_path()
        if profile is None or not target:
            self._status_label.setText(
                "Pick a destination first (or click + Add to create one)."
            )
            return
        self.sync_scan_requested.emit(
            profile,
            target,
            self.selected_sync_mode(),
            self.deep_verify_enabled(),
            self.selected_destination_id(),
        )

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
        include_roms = self._include_roms_cb.isChecked()
        lines: list[str] = []
        if include_roms:
            lines.append(
                f"Exporting {preview.file_count} ROM(s) "
                f"({_format_bytes(preview.total_size_bytes)}) "
                f"across {len(preview.by_system)} system(s)."
            )
        else:
            # Artwork-only mode: ROM bytes won't move. Re-frame the
            # preview around what *will* happen — covers refreshed,
            # gamelists rebuilt — so the user isn't misled by a
            # "Exporting 38,120 ROMs (240 GB)" headline when none of
            # those bytes will actually be copied.
            lines.append(
                f"Artwork-only mode — refreshing covers + "
                f"gamelist.xml for {len(preview.by_system)} system(s) "
                f"covering {preview.file_count} game(s). "
                f"No ROM bytes will be copied."
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

    def on_progress(self, current: int, total: int, label: str) -> None:
        """Slot — driven by ExportWorker progress signal.

        Two phases, each with its own label scale:

        * Phase 1 (ROM copy): ``current`` / ``total`` count files and
          ``label`` is verb-prefixed by the exporter ("Copying foo.sfc").
        * Phase 2 (sidecars): ``current`` / ``total`` count systems and
          ``label`` reads "Refreshing sidecars: <system_id>".

        Render the label as authoritative — the previous hard-coded
        "Exporting N of M" prefix produced a stuck-at-100% UX once
        phase 1 finished because nothing updated the label during the
        artwork pass.
        """
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
            self._status_label.setText(f"{label} ({current} of {total})")
        else:
            # Indeterminate phase tick — just drive the label.
            self._status_label.setText(label)

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
