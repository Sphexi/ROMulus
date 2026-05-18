"""Shared layout helpers for QProgressDialog subclasses.

Every long-running operation (Quick Scan, Heavy Scan, Enrich Metadata,
Find Covers, Destination Scan) reports progress with the file path of
the currently-processing item. UNC and deep nested paths can run to
several hundred characters, and the default ``QProgressDialog`` autosizes
to fit its label — that produces a 1200+ px wide window for any long
path and leaves it there for the rest of the run (Qt's progress dialog
sets a one-way minimum width as it grows; it never shrinks back).

:func:`pin_progress_dialog_layout` undoes that:

1. Replaces the dialog's internal label with a :class:`QLabel` that
   wraps long text — so a long path produces additional vertical lines
   instead of stretching the window horizontally.
2. Pins the dialog to a fixed reasonable width.
3. Raises the minimum height enough that the bar + a few wrapped lines
   of label text all fit without scrolling.

Apply in each progress dialog's ``__init__`` after the base class
constructor.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QProgressDialog

#: Comfortable target width for every long-running progress dialog.
#: Roughly the width of an EXPLAIN-friendly file path on a modern
#: laptop display without dominating the workspace; matches the
#: width-feel of the other modal dialogs in the app (SyncPreview,
#: OrganizePreview).
PROGRESS_DIALOG_WIDTH = 560

#: Minimum vertical room for the label + the progress bar + the cancel
#: button without making the dialog so tall it looks empty when the
#: label happens to be short.
PROGRESS_DIALOG_MIN_HEIGHT = 180


def pin_progress_dialog_layout(dialog: QProgressDialog) -> QLabel:
    """Install a word-wrapping label and pin the dialog's width / min height.

    Returns the new label so the caller can hand-tune alignment or
    styling if needed. ``setLabelText`` on the dialog continues to
    work — Qt forwards the call to whatever widget is registered as
    the label, so existing on_progress / on_finished slots don't need
    changes.

    Idempotent: calling twice replaces the label cleanly because
    ``QProgressDialog.setLabel`` takes ownership of the new widget
    and deletes the previous one.
    """
    label = QLabel(dialog)
    label.setWordWrap(True)
    label.setAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
    )
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
    )
    label.setText(dialog.labelText())
    # ``setLabel`` takes ownership of the widget and deletes the
    # default internal label Qt creates in QProgressDialog's ctor.
    dialog.setLabel(label)

    dialog.setFixedWidth(PROGRESS_DIALOG_WIDTH)
    dialog.setMinimumHeight(PROGRESS_DIALOG_MIN_HEIGHT)
    return label
