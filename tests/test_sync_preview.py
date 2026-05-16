"""Tests for the SyncPreviewDialog UI (sync-design §6.2, §6.3).

Headless Qt tests via the ``qapp`` fixture in :mod:`conftest`. Each test
constructs a synthetic :class:`SyncPlan` rather than running the full diff
engine so the test stays focused on the dialog's behaviour:

* Bucketed tree with one section per action kind.
* Per-row checkbox toggling.
* Conflict-policy dropdown emits the right resolution value.
* Destructive-action double-confirm sequence fires when the plan contains
  a delete or overwrite, and the apply signal only emits after both
  prompts are accepted.
* Non-destructive plans skip the second prompt entirely.
"""

from __future__ import annotations

from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from romulus.core.sync import (
    ACTION_CONFLICT,
    ACTION_COPY_TO_DEST,
    ACTION_DELETE_DEST,
    ACTION_IDENTICAL,
    CONFLICT_RESOLUTION_LOCAL,
    CONFLICT_RESOLUTION_SKIP,
    SyncAction,
    SyncPlan,
)
from romulus.ui.sync_preview import SyncPreviewDialog


def _plan_with(actions: list[SyncAction], mode: str = "push_merge") -> SyncPlan:
    return SyncPlan(dest_id=1, mode=mode, actions=actions, conflict_policy="skip")  # type: ignore[arg-type]


def _add_action(rel: str = "snes/A.sfc") -> SyncAction:
    return SyncAction(
        kind=ACTION_COPY_TO_DEST,
        rel_path=rel,
        local_path=f"/lib/{rel}",
        dest_path=f"/dest/{rel}",
        size_bytes=1024,
        rom_id=1,
        game_id=1,
        system_id="snes",
    )


def _delete_action(rel: str = "snes/Orphan.sfc") -> SyncAction:
    return SyncAction(
        kind=ACTION_DELETE_DEST,
        rel_path=rel,
        local_path="",
        dest_path=f"/dest/{rel}",
        size_bytes=2048,
        rom_id=None,
        game_id=None,
        system_id="snes",
    )


def _conflict_action(
    rel: str = "snes/G.sfc", resolution: str = CONFLICT_RESOLUTION_SKIP
) -> SyncAction:
    return SyncAction(
        kind=ACTION_CONFLICT,
        rel_path=rel,
        local_path=f"/lib/{rel}",
        dest_path=f"/dest/{rel}",
        size_bytes=512,
        rom_id=2,
        game_id=2,
        system_id="snes",
        conflict_resolution=resolution,
    )


class TestDialogBucketing:
    def test_opens_with_action_tree(self, qapp) -> None:
        plan = _plan_with([_add_action(), _add_action("snes/B.sfc")])
        dialog = SyncPreviewDialog(plan, "Test target")
        # Two leaves under "To add to dest".
        assert dialog._model.rowCount() == 1  # one section
        section = dialog._model.invisibleRootItem().child(0, 0)
        assert section is not None
        assert section.rowCount() == 2

    def test_buckets_split_by_action_kind(self, qapp) -> None:
        plan = _plan_with(
            [_add_action(), _delete_action(), _conflict_action()]
        )
        dialog = SyncPreviewDialog(plan, "Test target")
        assert dialog._model.rowCount() == 3

    def test_empty_plan_disables_apply(self, qapp) -> None:
        plan = _plan_with([])
        dialog = SyncPreviewDialog(plan, "Test target")
        assert dialog._apply_btn.isEnabled() is False


class TestCheckboxes:
    def test_default_checked_for_action_rows(self, qapp) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        items = dialog._iter_action_items()
        assert len(items) == 1
        assert items[0].checkState() == Qt.CheckState.Checked

    def test_identical_defaults_unchecked(self, qapp) -> None:
        identical = SyncAction(kind=ACTION_IDENTICAL, rel_path="x")
        plan = _plan_with([identical])
        dialog = SyncPreviewDialog(plan, "Test target")
        items = dialog._iter_action_items()
        assert items[0].checkState() == Qt.CheckState.Unchecked

    def test_select_all_and_deselect_all(self, qapp) -> None:
        plan = _plan_with([_add_action(), _add_action("snes/B.sfc")])
        dialog = SyncPreviewDialog(plan, "Test target")
        dialog._on_deselect_all()
        items = dialog._iter_action_items()
        assert all(i.checkState() == Qt.CheckState.Unchecked for i in items)
        dialog._on_select_all()
        assert all(i.checkState() == Qt.CheckState.Checked for i in items)

    def test_approved_actions_filters_unchecked(self, qapp) -> None:
        a = _add_action("snes/A.sfc")
        b = _add_action("snes/B.sfc")
        plan = _plan_with([a, b])
        dialog = SyncPreviewDialog(plan, "Test target")
        items = dialog._iter_action_items()
        items[0].setCheckState(Qt.CheckState.Unchecked)
        approved = dialog.approved_actions()
        rel_paths = {x.rel_path for x in approved}
        assert rel_paths == {"snes/B.sfc"}


class TestConflictPolicyDropdown:
    def test_conflict_row_has_combo_widget(self, qapp) -> None:
        plan = _plan_with(
            [_conflict_action(resolution=CONFLICT_RESOLUTION_SKIP)],
            mode="two_way",
        )
        dialog = SyncPreviewDialog(plan, "Test target")
        header = dialog._model.invisibleRootItem().child(0, 0)
        # Column 3 is the Policy column where the combo lives.
        policy_index = dialog._model.indexFromItem(header.child(0, 3))
        widget = dialog._tree.indexWidget(policy_index)
        assert widget is not None

    def test_policy_change_mutates_action(self, qapp) -> None:
        action = _conflict_action(resolution=CONFLICT_RESOLUTION_SKIP)
        plan = _plan_with([action], mode="two_way")
        dialog = SyncPreviewDialog(plan, "Test target")
        header = dialog._model.invisibleRootItem().child(0, 0)
        policy_index = dialog._model.indexFromItem(header.child(0, 3))
        combo = dialog._tree.indexWidget(policy_index)
        assert combo is not None
        # Find "Local wins" item.
        for idx in range(combo.count()):  # type: ignore[attr-defined]
            if combo.itemData(idx) == CONFLICT_RESOLUTION_LOCAL:  # type: ignore[attr-defined]
                combo.setCurrentIndex(idx)  # type: ignore[attr-defined]
                break
        assert action.conflict_resolution == CONFLICT_RESOLUTION_LOCAL


class TestDestructiveDoubleConfirm:
    def test_non_destructive_plan_skips_both_prompts(self, qapp) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        with patch.object(QMessageBox, "question") as mock_q:
            received: list = []
            dialog.actions_approved.connect(lambda approved: received.append(approved))
            dialog._on_apply_clicked()
            # Neither destructive prompt should have fired.
            assert mock_q.call_count == 0
            # The apply signal still emits with the approved actions.
            assert len(received) == 1

    def test_destructive_plan_fires_both_prompts(self, qapp) -> None:
        plan = _plan_with([_delete_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        with patch.object(
            QMessageBox,
            "question",
            side_effect=[
                QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Yes,
            ],
        ) as mock_q:
            received: list = []
            dialog.actions_approved.connect(lambda approved: received.append(approved))
            dialog._on_apply_clicked()
            assert mock_q.call_count == 2
            assert len(received) == 1

    def test_destructive_first_prompt_cancel_aborts(self, qapp) -> None:
        plan = _plan_with([_delete_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as mock_q:
            received: list = []
            dialog.actions_approved.connect(lambda approved: received.append(approved))
            dialog._on_apply_clicked()
            # First prompt fired; user cancelled — second prompt never runs.
            assert mock_q.call_count == 1
            # No actions emitted.
            assert received == []

    def test_destructive_second_prompt_cancel_aborts(self, qapp) -> None:
        plan = _plan_with([_delete_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        with patch.object(
            QMessageBox,
            "question",
            side_effect=[
                QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Cancel,
            ],
        ) as mock_q:
            received: list = []
            dialog.actions_approved.connect(lambda approved: received.append(approved))
            dialog._on_apply_clicked()
            assert mock_q.call_count == 2
            assert received == []

    def test_conflict_with_overwrite_resolution_is_destructive(
        self, qapp
    ) -> None:
        plan = _plan_with(
            [_conflict_action(resolution=CONFLICT_RESOLUTION_LOCAL)],
            mode="two_way",
        )
        dialog = SyncPreviewDialog(plan, "Test target")
        with patch.object(
            QMessageBox,
            "question",
            side_effect=[
                QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Yes,
            ],
        ) as mock_q:
            received: list = []
            dialog.actions_approved.connect(lambda approved: received.append(approved))
            dialog._on_apply_clicked()
            # Conflict overwrite triggers the destructive flow.
            assert mock_q.call_count == 2

    def test_conflict_with_skip_resolution_is_non_destructive(
        self, qapp
    ) -> None:
        plan = _plan_with(
            [_conflict_action(resolution=CONFLICT_RESOLUTION_SKIP)],
            mode="two_way",
        )
        dialog = SyncPreviewDialog(plan, "Test target")
        with patch.object(QMessageBox, "question") as mock_q:
            received: list = []
            dialog.actions_approved.connect(lambda approved: received.append(approved))
            dialog._on_apply_clicked()
            assert mock_q.call_count == 0
            assert len(received) == 1


class TestProgressHooks:
    def test_on_progress_updates_label(self, qapp) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        dialog.on_progress(1, 10, "snes/Game.sfc")
        # Summary label should now mention the file.
        assert "Game.sfc" in dialog._summary_label.text()

    def test_on_finished_shows_summary(self, qapp) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        dialog.on_finished(5, 1, 0)
        assert "Applied 5" in dialog._summary_label.text()

    def test_on_failed_shows_error(self, qapp) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, "Test target")
        dialog.on_failed("Sync cancelled")
        assert "cancelled" in dialog._summary_label.text().lower()
