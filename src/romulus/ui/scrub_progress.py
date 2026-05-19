"""Verify Library — analyse-phase progress dialog.

The scrub flow has two phases, each with its own progress UI:

* **Analyse** (this dialog): walks every roms row + stats each file.
  Read-only, safely cancellable. Driven by :class:`ScrubAnalyseWorker`.
* **Apply** (`ScrubPreviewDialog.on_progress`): runs the approved
  actions through per-bucket SAVEPOINTs. The preview dialog hosts its
  own progress bar once the user clicks Apply, so this dialog only
  covers the analyse phase.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from romulus.ui._progress_layout import pin_progress_dialog_layout


class ScrubAnalyseProgressDialog(QProgressDialog):
    """Progress dialog driven by ScrubAnalyseWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Preparing to verify library…", "Cancel", 0, 0, parent)
        self.setWindowTitle("Verify Library")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)
        pin_progress_dialog_layout(self)

    def on_progress(self, current: int, total: int, filename: str) -> None:
        if total > 0:
            self.setMaximum(total)
            self.setValue(current)
            self.setLabelText(f"Checking {current} of {total}\n{filename}")
        else:
            self.setLabelText(filename)

    def on_failed(self, message: str) -> None:
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
