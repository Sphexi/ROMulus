"""Enrichment progress dialog — fetches covers and metadata in the background.

Driven by `EnrichWorker` signals. Unlike the scan dialog, the enrich worker
reports (current, total, title) so we can show a determinate progress bar
once the orchestrator has decided how many games to walk.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget


class EnrichProgressDialog(QProgressDialog):
    """Determinate progress dialog driven by EnrichWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Preparing enrichment...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Enrich Library")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)

    def on_progress(self, current: int, total: int, title: str) -> None:
        """Slot — bump the progress bar and show the current game title."""
        if total > 0:
            self.setMaximum(total)
            self.setValue(current)
        self.setLabelText(f"Enriching {current} of {total}\n{title}")

    def on_finished(
        self,
        games_processed: int,
        metadata_added: int,
        covers_added: int,
    ) -> None:
        """Slot — show the final summary and switch Cancel into Close."""
        self.setLabelText(
            f"Enrichment complete.\n"
            f"Games processed: {games_processed}\n"
            f"Metadata added: {metadata_added}\n"
            f"Covers added: {covers_added}"
        )
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — show an error message and switch Cancel into Close."""
        self.setLabelText(message)
        self.setCancelButtonText("Close")
