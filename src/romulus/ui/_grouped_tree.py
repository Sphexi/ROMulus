"""Shared mixin: tri-state group headers + right-click bulk toggle for QTreeView checkbox dialogs.

Used by the three preview dialogs that present a grouped list of
checkable actions: :class:`romulus.ui.organize_preview.OrganizePreviewDialog`,
:class:`romulus.ui.sync_preview.SyncPreviewDialog`, and
:class:`romulus.ui.scrub_dialog.ScrubPreviewDialog`.

The mixin adds two affordances on top of the existing per-row checkboxes:

1. **Tri-state group headers.** Each top-level row in the model becomes a
   checkable header showing ``Checked`` (all children checked),
   ``Unchecked`` (none), or ``PartiallyChecked`` (some). Clicking the
   header toggles every child between Checked and Unchecked.

2. **Right-click bulk toggle.** Right-clicking anywhere on a group
   (header or child) pops a menu with "Select all in this group" /
   "Deselect all in this group". Equivalent to the tri-state click but
   discoverable for users who don't think to click the header itself.

Both paths share the same cascade / re-sync code so user expectations
about feedback are consistent.

Subclasses must provide:

* ``self._tree`` — a ``QTreeView``.
* ``self._model`` — the ``QStandardItemModel`` backing the tree.
* A populated model where each top-level row is a group, with the
  checkable child item in column 0.

After populating the model, the subclass calls
:meth:`GroupedCheckboxTreeMixin._install_group_toggle` to wire the
behaviour. Headers whose children are all non-checkable (e.g. the
Organize "Collisions" bucket) are silently kept non-checkable.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QStandardItem
from PySide6.QtWidgets import QMenu


class GroupedCheckboxTreeMixin:
    """Adds tri-state group headers + right-click toggle to a checkbox tree.

    Internal attribute prefix ``_gct_`` avoids collisions with the host
    dialog's own attributes. The mixin assumes the host already owns
    ``_tree`` and ``_model`` — it doesn't construct them.
    """

    #: Re-entrancy guard. The mixin mutates child check-states while
    #: handling ``itemChanged`` on a header (and vice-versa). Without
    #: this flag the cascade re-fires the signal for every child and
    #: each fire tries to re-sync the header, producing O(n²) work and
    #: subtle flicker. The flag is set as a Python attribute via
    #: :meth:`_install_group_toggle` so it survives the mixin's
    #: stateless protocol.
    _gct_recursing: bool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _install_group_toggle(self) -> None:
        """Wire tri-state headers + the right-click bulk-toggle menu.

        Idempotent under repeated calls — the ``_gct_installed`` flag
        prevents double-connecting the signal handlers when a dialog
        rebuilds its model. (Calling ``disconnect`` unconditionally is
        unsafe under PySide6: a missing connection emits a stderr
        ``libpyside: Failed to disconnect`` warning rather than
        raising, so try/except can't suppress it cleanly.)
        """
        self._gct_recursing = False
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            header = root.child(i, 0)
            if header is None:
                continue
            self._gct_make_header_checkable(header)
        if not getattr(self, "_gct_installed", False):
            self._model.itemChanged.connect(self._gct_on_item_changed)
            self._tree.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu
            )
            self._tree.customContextMenuRequested.connect(
                self._gct_on_context_menu
            )
            self._gct_installed = True

    # ------------------------------------------------------------------
    # Header / child helpers
    # ------------------------------------------------------------------

    def _gct_make_header_checkable(self, header: QStandardItem) -> None:
        """Promote ``header`` to tri-state-checkable, or leave alone if all children unchecked-only.

        A header whose every child is non-checkable (e.g. the Organize
        Collisions bucket) gets ``setCheckable(False)`` — there's
        nothing to toggle, so showing a useless checkbox would
        actively confuse the user.
        """
        has_checkable = False
        for j in range(header.rowCount()):
            child = header.child(j, 0)
            if child is not None and child.isCheckable():
                has_checkable = True
                break
        if not has_checkable:
            header.setCheckable(False)
            return
        header.setCheckable(True)
        header.setUserTristate(True)
        # Tri-state controls aren't editable themselves — the click
        # cycles state. Editable=False keeps the label from going into
        # rename mode on double-click.
        header.setEditable(False)
        # Sync the initial state so it reflects whatever the children
        # were populated with.
        self._gct_sync_header_state(header)

    def _gct_sync_header_state(self, header: QStandardItem) -> None:
        """Update ``header``'s check-state to mirror its children."""
        checked = 0
        unchecked = 0
        total = 0
        for j in range(header.rowCount()):
            child = header.child(j, 0)
            if child is None or not child.isCheckable():
                continue
            total += 1
            if child.checkState() == Qt.CheckState.Checked:
                checked += 1
            else:
                unchecked += 1
        if total == 0:
            return
        if checked == total:
            new_state = Qt.CheckState.Checked
        elif unchecked == total:
            new_state = Qt.CheckState.Unchecked
        else:
            new_state = Qt.CheckState.PartiallyChecked
        if header.checkState() != new_state:
            header.setCheckState(new_state)

    def _gct_cascade_header(
        self, header: QStandardItem, state: Qt.CheckState
    ) -> None:
        """Set every checkable child of ``header`` to ``state``."""
        self._gct_recursing = True
        try:
            for j in range(header.rowCount()):
                child = header.child(j, 0)
                if child is not None and child.isCheckable():
                    child.setCheckState(state)
        finally:
            self._gct_recursing = False
        self._gct_sync_header_state(header)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _gct_on_item_changed(self, item: QStandardItem) -> None:
        """Cascade header toggles to children; re-sync header on child changes."""
        if self._gct_recursing:
            return
        if not item.isCheckable():
            return
        # PartiallyChecked is only ever set by the mixin itself in
        # response to mixed children — never by the user. Ignore it
        # here to avoid a re-entrant cascade after a user-driven
        # header click.
        if item.parent() is None:
            # Header — cascade if the new state is binary.
            state = item.checkState()
            if state == Qt.CheckState.PartiallyChecked:
                return
            self._gct_cascade_header(item, state)
        else:
            # Child — re-sync the parent header's tri-state.
            header = item.parent()
            if header is None or not header.isCheckable():
                return
            self._gct_recursing = True
            try:
                self._gct_sync_header_state(header)
            finally:
                self._gct_recursing = False

    def _gct_on_context_menu(self, pos: QPoint) -> None:
        """Pop the 'Select / Deselect all in this group' menu at ``pos``."""
        index = self._tree.indexAt(pos)
        if not index.isValid():
            return
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        # Resolve the group header for the clicked row — either the
        # item IS a header (no parent) or it's a child (header is the
        # parent).
        header = (
            self._model.item(index.row(), 0)
            if item.parent() is None
            else item.parent()
        )
        if header is None or not header.isCheckable():
            return
        menu = QMenu(self._tree)
        select_action = menu.addAction("Select all in this group")
        deselect_action = menu.addAction("Deselect all in this group")
        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen == select_action:
            self._gct_cascade_header(header, Qt.CheckState.Checked)
        elif chosen == deselect_action:
            self._gct_cascade_header(header, Qt.CheckState.Unchecked)
