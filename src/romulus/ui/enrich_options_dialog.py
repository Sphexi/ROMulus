"""Pre-run prompt for batch enrichment.

Surfaces the silent filters in ``get_games_needing_enrichment`` and
the online/offline split in the metadata orchestrator as explicit
user choices when enrichment is about to touch a lot of games
(per-system, per-collection, or full library). The single-game right-
click path bypasses this dialog entirely — see ``MainWindow._enrich_scoped``.

Default state: ``fuzzy`` and ``re-enrich`` start unchecked (safe
historic behaviour); ``online sources`` starts checked (otherwise an
unaware user might bypass the option and silently lose every chance
of enrichment for games not in the bundled offline databases).
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

    Three checkboxes:

    * ``Also enrich fuzzy-matched games`` — opt in to enriching ROMs that
      were only matched by filename heuristics, not DAT hash. Risky: a
      wrong filename guess attaches wrong metadata.
    * ``Re-attempt games that already have metadata`` — opt in to
      re-running every provider against games whose metadata row was
      filled by a prior enrich pass. Useful after configuring a new
      provider (e.g. TheGamesDB) when you want to top up partial hits.
    * ``Also try online metadata sources`` — when *unchecked*, only the
      bundled offline sources (libretro-database, GameDB, LaunchBox XML)
      run; games with no offline match get skipped without ever
      contacting Hasheous, ScreenScraper, or TheGamesDB. Checked by
      default because that's the historic behaviour.

    Read :attr:`include_fuzzy`, :attr:`include_already_enriched`, and
    :attr:`include_online` after :meth:`exec` returns
    :attr:`QDialog.Accepted`.
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
            "that haven't been enriched yet, and consults online metadata "
            "sources when the bundled offline databases miss. Adjust the "
            "boxes below to change this run."
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

        self.online_box = QCheckBox(
            "Also try online metadata sources "
            "(Hasheous, ScreenScraper, TheGamesDB) when offline databases miss"
        )
        # Checked by default — the alternative is silently doing nothing
        # for most non-cartridge games, which is not what the historic
        # global Enrich did.
        self.online_box.setChecked(True)

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
        layout.addWidget(self.online_box)
        layout.addWidget(buttons)

    @property
    def include_fuzzy(self) -> bool:
        """Whether the user opted in to fuzzy-matched games."""
        return self.fuzzy_box.isChecked()

    @property
    def include_already_enriched(self) -> bool:
        """Whether the user opted in to re-running already-enriched games."""
        return self.reenrich_box.isChecked()

    @property
    def include_online(self) -> bool:
        """Whether online metadata providers should run for this batch."""
        return self.online_box.isChecked()
