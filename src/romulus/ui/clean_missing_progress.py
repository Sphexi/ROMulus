"""Clean Missing Entries progress dialog — driven by ``CleanMissingWorker``.

Switches to determinate mode once the worker reports a non-zero total, then
shows the phase label ("Deleting dependent rows…" / "Pruning orphan
games…") above a chunk-progress bar. Cancel is allowed during the
chunked dependent-row delete (cooperative — checked on every progress
tick) but is silently ignored by the worker during the final
``DELETE FROM roms`` and ``prune_orphan_games`` statements, which are
not safely interruptible.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from romulus.ui._progress_layout import pin_progress_dialog_layout


class CleanMissingProgressDialog(QProgressDialog):
    """Progress dialog driven by CleanMissingWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Preparing to clean missing entries…", "Cancel", 0, 0, parent)
        self.setWindowTitle("Clean Missing Entries")
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
            self.setLabelText(f"{label}\n{current} of {total}")
        else:
            self.setLabelText(label)

    def on_finished(self, deleted: int, pruned: int) -> None:
        """Slot — stop the bar, show the final summary, switch to Close."""
        self.setRange(0, 1)
        self.setValue(1)
        if deleted == 0 and pruned == 0:
            self.setLabelText(
                "✓ Nothing to clean — every ROM in the database was already "
                "present on disk."
            )
        else:
            self.setLabelText(
                f"✓ Removed {deleted} missing entries.\n"
                f"Pruned {pruned} orphan games."
            )
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — stop the bar, show an error message, switch to Close."""
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
