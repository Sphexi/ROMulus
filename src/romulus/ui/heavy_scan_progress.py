"""Heavy Scan progress dialog — hashing and DAT matching in the background.

Driven by `HeavyScanWorker` signals. Progress is indeterminate during the
initial DAT load and determinate during the hash phase.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget


class HeavyScanProgressDialog(QProgressDialog):
    """Progress dialog driven by HeavyScanWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Preparing heavy scan...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Heavy Scan")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)

    def on_progress(self, hashed: int, total: int, filename: str) -> None:
        """Slot — update label; switch to determinate mode once total is known."""
        if total > 0:
            self.setMaximum(total)
            self.setValue(hashed)
            self.setLabelText(f"Hashing {hashed} of {total}\n{filename}")
        else:
            self.setLabelText(filename)

    def on_finished(
        self,
        total_hashed: int,
        total_matched: int,
        errors: int,
    ) -> None:
        """Slot — show the final summary and switch Cancel into Close."""
        self.setRange(0, 1)
        self.setValue(1)
        self.setLabelText(
            f"Heavy Scan complete.\n"
            f"ROMs hashed: {total_hashed}\n"
            f"DAT matches found: {total_matched}\n"
            f"Errors: {errors}"
        )
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — show an error message and switch Cancel into Close."""
        self.setRange(0, 1)
        self.setValue(1)
        self.setLabelText(message)
        self.setCancelButtonText("Close")
