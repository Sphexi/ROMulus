"""Local cover discovery progress dialog.

Driven by :class:`~romulus.ui.workers.LocalCoverFinderWorker` signals.
Uses a determinate QProgressBar once the total ROM count is known.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget


class LocalCoverProgressDialog(QProgressDialog):
    """Determinate progress dialog driven by LocalCoverFinderWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Scanning for local cover images...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Find Local Covers")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)

    def on_progress(self, current: int, total: int, filename: str) -> None:
        """Slot — bump the progress bar and display the current ROM filename.

        Args:
            current: Number of ROMs processed so far.
            total: Total number of ROMs to process.
            filename: Basename of the ROM currently being checked.
        """
        if total > 0:
            self.setMaximum(total)
            self.setValue(current)
        self.setLabelText(
            f"Scanning {current} of {total}\n{filename}"
        )

    def on_finished(
        self,
        roms_scanned: int,
        covers_found: int,
        covers_skipped: int,
        errors: int,
    ) -> None:
        """Slot — show a completion summary and switch to Close.

        Args:
            roms_scanned: Total ROMs examined.
            covers_found: New cover rows inserted.
            covers_skipped: Rows skipped because they already existed.
            errors: Non-fatal errors encountered.
        """
        self.setRange(0, 1)
        self.setValue(1)
        error_line = f"\nErrors: {errors}" if errors else ""
        self.setLabelText(
            f"✓ Local cover discovery complete.\n"
            f"ROMs scanned: {roms_scanned}\n"
            f"Covers found: {covers_found}\n"
            f"Already linked (skipped): {covers_skipped}"
            f"{error_line}"
        )
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — show an error message and switch to Close.

        Args:
            message: Human-readable failure description.
        """
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
