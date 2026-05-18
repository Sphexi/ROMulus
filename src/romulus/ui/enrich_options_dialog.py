"""Pre-run prompt for batch enrichment.

Surfaces the two silent filters in ``get_games_needing_enrichment`` as
explicit user choices when enrichment is about to touch a lot of games
(per-system, per-collection, or full library). The single-game right-
click path bypasses this dialog entirely — see ``MainWindow._enrich_scoped``.

The default state for both checkboxes is *unchecked* — that matches the
historic enricher behaviour (only DAT-verified, no re-enrich) so users
who click through without reading get the safe path.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class EnrichOptionsDialog(QDialog):
    """Ask the user which enrichment filters to loosen for a batch run.

    Two checkboxes:

    * ``Also enrich fuzzy-matched games`` — opt in to enriching ROMs that
      were only matched by filename heuristics, not DAT hash. Risky: a
      wrong filename guess attaches wrong metadata.
    * ``Re-attempt games that already have metadata`` — opt in to
      re-running every provider against games whose metadata row was
      filled by a prior enrich pass. Useful after configuring a new
      provider (e.g. TheGamesDB) when you want to top up partial hits.

    Read :attr:`include_fuzzy` and :attr:`include_already_enriched`
    after :meth:`exec` returns :attr:`QDialog.Accepted`.
    """

    def __init__(
        self,
        scope_label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Enrichment options")

        intro = QLabel(
            f"About to enrich {scope_label}.\n\n"
            "By default the enricher only touches DAT-verified games "
            "that haven't been enriched yet. Tick a box below to loosen "
            "those filters for this run."
        )
        intro.setWordWrap(True)

        self.fuzzy_box = QCheckBox(
            "Also enrich fuzzy-matched games "
            "(risky — wrong filename guesses attach wrong metadata)"
        )
        self.fuzzy_box.setChecked(False)

        self.reenrich_box = QCheckBox(
            "Re-attempt enrichment on games that already have metadata"
        )
        self.reenrich_box.setChecked(False)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Enrich")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.fuzzy_box)
        layout.addWidget(self.reenrich_box)
        layout.addWidget(buttons)

    @property
    def include_fuzzy(self) -> bool:
        """Whether the user opted in to fuzzy-matched games."""
        return self.fuzzy_box.isChecked()

    @property
    def include_already_enriched(self) -> bool:
        """Whether the user opted in to re-running already-enriched games."""
        return self.reenrich_box.isChecked()
