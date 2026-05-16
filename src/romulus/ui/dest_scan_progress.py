"""Destination-scan progress dialog.

Driven by :class:`~romulus.ui.workers.DestInventoryWorker` signals. The walk
emits ``progress(current, total, label)`` per dest file inspected; ``total``
is unknown when the worker starts (the walk has to find the file list
first), so the dialog opens in indeterminate mode (``range(0, 0)``) and
switches to determinate as soon as the worker reports a real total.

Pattern matches the other scan-progress dialogs in this package
(:mod:`romulus.ui.scan_progress`, :mod:`romulus.ui.heavy_scan_progress`,
:mod:`romulus.ui.local_cover_progress`) so MainWindow can wire it up with
the same boilerplate it uses everywhere else.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget


class DestScanProgressDialog(QProgressDialog):
    """Indeterminate-to-determinate progress dialog for the dest walk."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Scanning destination...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Scan Destination")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        # Show the dialog immediately so the user gets visual feedback
        # before the first progress tick — the previous bug was the user
        # seeing a frozen main window for ~30s with no indicator.
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)

    def on_progress(self, current: int, total: int, label: str) -> None:
        """Slot — bump the bar and show the current dest-relative path.

        Args:
            current: Number of dest files inspected so far.
            total: Total to inspect, or 0 while the walk is still
                discovering the file list (the dialog stays indeterminate
                in that case).
            label: Forward-slash relative path of the file being
                inspected.
        """
        if total > 0:
            self.setMaximum(total)
            self.setValue(current)
        self.setLabelText(
            f"Scanning {current} of {total}\n{label}"
            if total
            else f"Walking destination...\n{label}"
        )

    def on_finished(self, _inventory: object) -> None:
        """Slot — flip the bar to 100% and switch the button to Close.

        The actual inventory object is consumed by MainWindow's slot that
        opens the preview dialog; this hook just retires the progress UI.
        """
        self.setRange(0, 1)
        self.setValue(1)
        self.setLabelText("✓ Destination scan complete.")
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — show an error message and switch to Close."""
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
