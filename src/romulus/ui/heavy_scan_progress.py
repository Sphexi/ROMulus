"""Heavy Scan progress dialog — hashing and DAT matching in the background.

Driven by `HeavyScanWorker` signals. Progress is indeterminate during the
initial DAT load and determinate during the hash phase.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from romulus.ui._progress_layout import pin_progress_dialog_layout


class HeavyScanProgressDialog(QProgressDialog):
    """Progress dialog driven by HeavyScanWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Preparing heavy scan...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Heavy Scan")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)
        pin_progress_dialog_layout(self)

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
        """Slot — stop the spinner, show the final summary, switch to Close.

        The "cache up to date" case (no new hashes, no new matches, no
        errors) gets explicit wording so the user knows nothing was
        broken — heavy scan reuses cached hashes by (path, mtime, size)
        and silent no-ops on subsequent runs are normal.
        """
        self.setRange(0, 1)
        self.setValue(1)
        if total_hashed == 0 and total_matched == 0 and errors == 0:
            self.setLabelText(
                "✓ Heavy Scan complete — cache up to date.\n"
                "No ROMs needed re-hashing and every existing hash is "
                "already DAT-matched.\n\n"
                "Heavy Scan re-hashes a ROM only when its recorded "
                "modification time has drifted from when it was last "
                "hashed. To pick up file changes, run Quick Scan first "
                "— it re-stats every file and updates the modification "
                "time so the next Heavy Scan can detect the drift."
            )
        else:
            error_line = f"\nErrors: {errors}" if errors else ""
            self.setLabelText(
                f"✓ Heavy Scan complete.\n"
                f"ROMs hashed: {total_hashed}\n"
                f"New DAT matches: {total_matched}"
                f"{error_line}"
            )
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — stop the spinner, show an error message, switch to Close."""
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
