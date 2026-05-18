"""Metadata-enrichment progress dialog.

Driven by `EnrichWorker` signals. Reports per-game progress as the
orchestrator walks the eligible-game list. As of the metadata/covers
split, this dialog summarises *metadata* work only — covers are
handled by the separate Find Covers workflow.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget


class EnrichProgressDialog(QProgressDialog):
    """Determinate progress dialog driven by EnrichWorker signals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Preparing enrichment...", "Cancel", 0, 0, parent)
        self.setWindowTitle("Enrich Metadata")
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
        _covers_added: int,
    ) -> None:
        """Slot — stop the spinner, show the final summary, switch to Close.

        ``_covers_added`` is the third signal argument kept for
        signature compatibility with :class:`EnrichWorker`; it's always
        zero now (covers are a separate workflow) and isn't displayed.
        """
        self.setRange(0, 1)
        self.setValue(1)
        self.setLabelText(
            f"✓ Metadata enrichment complete.\n"
            f"Games processed: {games_processed}\n"
            f"Metadata added: {metadata_added}"
        )
        self.setCancelButtonText("Close")

    def on_failed(self, message: str) -> None:
        """Slot — stop the spinner, show an error message, switch to Close."""
        self.setRange(0, 1)
        self.setValue(0)
        self.setLabelText(f"✗ {message}")
        self.setCancelButtonText("Close")
