"""Cover discovery progress dialog (local + online phases).

Driven by :class:`~romulus.ui.workers.CoverFinderWorker` signals.
Uses a determinate QProgressBar once the total ROM count is known.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget


class LocalCoverProgressDialog(QProgressDialog):
    """Determinate progress dialog driven by CoverFinderWorker signals.

    Class name kept as ``LocalCoverProgressDialog`` to minimise import
    churn; the dialog title now reads "Find Covers" since this same
    widget drives the combined local + online workflow.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Scanning for cover images...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Find Covers")
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
        online_covers: int = 0,
    ) -> None:
        """Slot — show a completion summary and switch to Close.

        Args:
            roms_scanned: Total ROMs examined during the local phase.
            covers_found: New cover rows inserted from local images.
            covers_skipped: Rows skipped because they already existed.
            errors: Non-fatal errors encountered.
            online_covers: Cover rows added by the online libretro
                thumbnail phase (0 when that phase was disabled).
        """
        self.setRange(0, 1)
        self.setValue(1)
        error_line = f"\nErrors: {errors}" if errors else ""
        online_line = (
            f"\nOnline covers added: {online_covers}" if online_covers else ""
        )
        self.setLabelText(
            f"✓ Cover discovery complete.\n"
            f"ROMs scanned: {roms_scanned}\n"
            f"Covers found locally: {covers_found}\n"
            f"Already linked (skipped): {covers_skipped}"
            f"{online_line}"
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
