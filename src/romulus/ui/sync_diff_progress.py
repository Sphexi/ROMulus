"""Sync diff progress dialog — driven by :class:`BuildSyncPlanWorker`.

The destination scan is fast (handled by the cached
``DestInventoryWorker``), but the diff phase that follows can take a
beat on a large library: building the local match index, indexing the
inventory by fuzzy key, walking the action list. Without this dialog
the UI would freeze for that beat — ``build_plan`` used to run inside
a slot on the UI thread and produced a multi-second "not responding"
window on a 38 K-rom × 17 K-entry pairing.

The dialog runs in indeterminate mode while loading the library index,
then switches to determinate mode for the per-rom action walk once
``BuildSyncPlanWorker`` emits a total.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from romulus.ui._progress_layout import pin_progress_dialog_layout


class SyncDiffProgressDialog(QProgressDialog):
    """Progress dialog driven by BuildSyncPlanWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Computing sync diff…", "Cancel", 0, 0, parent)
        self.setWindowTitle("Computing sync diff")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)
        pin_progress_dialog_layout(self)

    def on_progress(self, current: int, total: int, label: str) -> None:
        """Slot — update label; switch to determinate mode once total > 0."""
        if total > 0:
            self.setMaximum(total)
            self.setValue(current)
            if total > 1:
                self.setLabelText(f"{label}\n{current} of {total}")
            else:
                self.setLabelText(label)
        else:
            self.setLabelText(label)

    def on_failed(self, message: str) -> None:
        """Slot — show an error message and switch to Close."""
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
