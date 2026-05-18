"""Pre-run prompt for the Find Covers workflow.

Asks the user which cover sources to consult before kicking off the
:class:`romulus.ui.workers.CoverFinderWorker`. Two checkboxes:

* ``Search for local covers`` — walk the library tree for image files
  alongside each ROM. Default *checked* (cheap, deterministic, offline).
* ``Search online for covers`` — fetch libretro thumbnails for every
  game in scope that doesn't already have one. Default *unchecked*
  (network traffic + cache writes; user opts in).

Mirrors :class:`romulus.ui.enrich_options_dialog.EnrichOptionsDialog`'s
shape so the two prompts feel consistent.
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


class CoverOptionsDialog(QDialog):
    """Ask the user which cover sources to consult for this run.

    Read :attr:`include_local` and :attr:`include_online` after
    :meth:`exec` returns :attr:`QDialog.Accepted`. The OK button is
    disabled when both checkboxes are unchecked — running with nothing
    selected does nothing.
    """

    def __init__(
        self,
        scope_label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cover discovery options")

        intro = QLabel(
            f"About to find covers for {scope_label}.\n\n"
            "Choose which sources to consult. Local discovery looks at "
            "image files inside your library folder; online discovery "
            "downloads libretro thumbnails for any games still missing "
            "covers afterwards."
        )
        intro.setWordWrap(True)

        self.local_box = QCheckBox(
            "Search for local covers "
            "(image files next to ROMs in the library)"
        )
        self.local_box.setChecked(True)

        self.online_box = QCheckBox(
            "Search online for covers "
            "(fetch libretro thumbnails for games still missing a cover)"
        )
        self.online_box.setChecked(False)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Find covers"
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        # Disable OK when neither source is selected — otherwise we'd
        # launch a worker that does nothing and reports zeros.
        self.local_box.toggled.connect(self._refresh_ok_state)
        self.online_box.toggled.connect(self._refresh_ok_state)
        self._refresh_ok_state()

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.local_box)
        layout.addWidget(self.online_box)
        layout.addWidget(self._buttons)

    def _refresh_ok_state(self) -> None:
        """Enable OK only when at least one source is selected."""
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is None:
            return
        ok.setEnabled(self.local_box.isChecked() or self.online_box.isChecked())

    @property
    def include_local(self) -> bool:
        """Whether the on-disk image-file walk should run."""
        return self.local_box.isChecked()

    @property
    def include_online(self) -> bool:
        """Whether libretro thumbnail fetching should run."""
        return self.online_box.isChecked()
