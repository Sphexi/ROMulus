"""Scan progress dialog — shows file count, current file, cancel button."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from romulus.ui._progress_layout import pin_progress_dialog_layout


class ScanProgressDialog(QProgressDialog):
    """Indeterminate progress dialog driven by ScanWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Starting scan...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Quick Scan")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)
        pin_progress_dialog_layout(self)

    def on_progress(self, count: int, filename: str) -> None:
        """Slot — update the label with the latest scan tick.

        Detects the post-walk phase by the literal Unicode ellipsis
        suffix on the label (the scanner emits "Marking missing
        entries…" / "Linking ROMs to games: …" / "Finalising scan
        history…" — none of which can collide with a real ROM
        filename). When detected, the Cancel button is removed:
        cancelling mid-rebuild would leave the DB inconsistent with
        disk, and the scanner has no safe abort points inside those
        phases. ``on_finished`` / ``on_failed`` re-create a "Close"
        button via ``setCancelButtonText`` when the worker is done.
        """
        if filename.endswith("…"):
            # QProgressDialog doesn't expose its cancel button
            # directly; ``setCancelButton(None)`` removes the button
            # entirely. Safe to call repeatedly — Qt no-ops when
            # there's already no cancel button.
            self.setCancelButton(None)
        self.setLabelText(f"Scanned {count} files\n{filename}")

    def on_finished(
        self,
        scan_id: int,  # noqa: ARG002 - signature mirrors worker signal
        files_found: int,
        files_with_system: int,
        files_skipped: int,
        systems_seen: list[str],  # noqa: ARG002
    ) -> None:
        """Slot — stop the spinner, show the final summary, switch to Close."""
        self.setRange(0, 1)
        self.setValue(1)
        self.setLabelText(
            f"✓ Scan complete.\n"
            f"Files found: {files_found}\n"
            f"Matched to a system: {files_with_system}\n"
            f"Skipped: {files_skipped}"
        )
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — stop the spinner, show an error message, switch to Close."""
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
