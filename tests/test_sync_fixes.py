"""Regression tests for the three v0.2.0 sync user-reported bugs.

Covers:

1. ``q.ensure_sync_destination_by_path`` — the idempotent helper that
   replaces the ``dest_id = -1`` sentinel that previously caused FOREIGN
   KEY constraint failures on every ``upsert_dest_inventory`` /
   ``insert_sync_plan`` insert.
2. The MainWindow one-shot slot upgrades ``dest_id = -1`` to a real id
   before the worker is spawned, so the inventory writes succeed.
3. :class:`DestScanProgressDialog` opens and forwards the worker's
   progress signal.
4. :class:`SyncPreviewDialog` shows the intro paragraph and the dynamic
   "Apply changes to <target>" button label.
5. Non-destructive plans still skip the destructive double-confirm.
6. Round-trip: open the preview, click Apply, all checked actions are
   emitted in the ``actions_approved`` signal.

Headless Qt via the ``qapp`` fixture (see :mod:`tests.conftest`).
"""

from __future__ import annotations

from unittest.mock import patch

from PySide6.QtWidgets import QMessageBox

from romulus.core.sync import (
    ACTION_COPY_TO_DEST,
    ACTION_DELETE_DEST,
    SyncAction,
    SyncPlan,
)
from romulus.db import queries as q
from romulus.ui.dest_scan_progress import DestScanProgressDialog
from romulus.ui.sync_preview import SyncPreviewDialog

# ---------------------------------------------------------------------------
# Bug 1 — q.ensure_sync_destination_by_path
# ---------------------------------------------------------------------------


class TestEnsureSyncDestinationByPath:
    def test_inserts_new_row_when_no_match(self, db) -> None:
        """No row at ``target_path`` -> a new row is inserted and id returned."""
        dest_id = q.ensure_sync_destination_by_path(
            db, "/mnt/anbernic", "anbernic"
        )
        assert dest_id > 0
        row = q.get_sync_destination(db, dest_id)
        assert row is not None
        assert row["target_path"] == "/mnt/anbernic"
        assert row["profile_id"] == "anbernic"
        assert row["name"].startswith("Quick Sync — ")

    def test_reuses_existing_row_for_same_path(self, db) -> None:
        """A second call with the same path returns the SAME id (idempotent)."""
        first = q.ensure_sync_destination_by_path(
            db, "/mnt/anbernic", "anbernic"
        )
        second = q.ensure_sync_destination_by_path(
            db, "/mnt/anbernic", "anbernic"
        )
        assert first == second
        # And only one row was created.
        rows = q.get_sync_destinations(db)
        assert len(rows) == 1

    def test_different_paths_get_different_ids(self, db) -> None:
        a = q.ensure_sync_destination_by_path(db, "/mnt/a", "anbernic")
        b = q.ensure_sync_destination_by_path(db, "/mnt/b", "anbernic")
        assert a != b
        assert len(q.get_sync_destinations(db)) == 2

    def test_name_collision_gets_numeric_suffix(self, db) -> None:
        """If the auto-generated name collides, suffix with " (N)" until unique."""
        # Manually create a row that would collide with the auto-generated name.
        q.insert_sync_destination(
            db,
            {
                "name": "Quick Sync — sdcard",
                "target_path": "/some/other/path",
                "profile_id": "batocera",
            },
        )
        # Now ensure_sync_destination_by_path for a NEW path whose basename
        # is "sdcard" should pick a non-colliding suffixed name.
        new_id = q.ensure_sync_destination_by_path(
            db, "/mnt/sdcard", "anbernic"
        )
        new_row = q.get_sync_destination(db, new_id)
        assert new_row is not None
        assert new_row["name"] == "Quick Sync — sdcard (2)"
        assert new_row["target_path"] == "/mnt/sdcard"

    def test_inventory_upsert_succeeds_with_ensured_id(self, db) -> None:
        """The FK error from Bug 1 — assert it's now fixed."""
        dest_id = q.ensure_sync_destination_by_path(
            db, "/mnt/anbernic", "anbernic"
        )
        # Previously this raised sqlite3.IntegrityError: FOREIGN KEY
        # constraint failed because dest_id was -1.
        q.upsert_dest_inventory(
            db,
            {
                "dest_id": dest_id,
                "rel_path": "Roms/gba/Game.gba",
                "size_bytes": 1024,
                "mtime": 0.0,
                "sha1": None,
                "rom_id": None,
                "game_id": None,
            },
        )
        db.commit()
        # Inventory row is now persisted.
        rows = db.execute(
            "SELECT COUNT(*) FROM dest_inventory WHERE dest_id = ?",
            (dest_id,),
        ).fetchone()
        assert rows[0] == 1


# ---------------------------------------------------------------------------
# Bug 1 — MainWindow upgrades dest_id=-1 before spawning the worker
# ---------------------------------------------------------------------------


class TestMainWindowOneShotUpgrade:
    def test_minus_one_dest_id_is_upgraded_before_worker_spawn(
        self, qapp, seeded_db, monkeypatch, tmp_path
    ) -> None:
        """When the dropdown emits dest_id=-1, the slot ensures a real id first.

        We patch :class:`DestInventoryWorker` so we can observe the
        constructor argument that the slot actually passes through — the
        old code passed ``-1`` straight through which is what broke every
        FK insert.
        """
        from romulus.models.profile import DestinationProfile
        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)
        # Stub out the export dialog field so the dropdown refresh side
        # effect doesn't blow up on a missing widget.
        window._export_dialog = None  # type: ignore[assignment]

        target_path = str(tmp_path / "dest")
        captured: dict[str, object] = {}

        class _FakeWorker:
            def __init__(
                self,
                _db_path,
                dest_id: int,
                target: str,
                *,
                deep_verify: bool = False,
            ) -> None:
                captured["dest_id"] = dest_id
                captured["target"] = target
                captured["deep_verify"] = deep_verify
                # Signals — give them no-op connect APIs.
                self.progress = _FakeSignal()
                self.finished_ok = _FakeSignal()
                self.failed = _FakeSignal()
                self.finished = _FakeSignal()

            def start(self) -> None:
                captured["started"] = True

            def cancel(self) -> None:  # pragma: no cover - never invoked
                pass

            def isRunning(self) -> bool:  # noqa: N802 - Qt API
                return False

            def deleteLater(self) -> None:  # noqa: N802 - Qt API
                pass

        class _FakeSignal:
            def connect(self, *_args, **_kwargs) -> None:
                pass

        monkeypatch.setattr(
            "romulus.ui.main_window.DestInventoryWorker", _FakeWorker
        )
        # Also stub out the progress dialog show() so no window pops in tests.
        monkeypatch.setattr(
            "romulus.ui.main_window.DestScanProgressDialog.show",
            lambda self: None,
        )

        # Build a minimal profile to pass through.
        profile = DestinationProfile(
            id="anbernic",
            name="Anbernic",
            base_path="Roms",
            gamelist_format="emulationstation_xml",
            artwork_subdir=None,
            artwork_filename_template="{stem}{ext}",
            multi_disc=None,
            systems={},
        )

        window._on_sync_scan_requested(profile, target_path, "push_merge", False, -1)

        assert captured["started"] is True
        assert captured["dest_id"] != -1, (
            "MainWindow must upgrade -1 to a real sync_destinations.id "
            "before the worker runs."
        )
        # The new id corresponds to a row that was actually inserted.
        row = q.get_sync_destination(seeded_db, int(captured["dest_id"]))
        assert row is not None
        assert row["target_path"] == target_path
        assert row["profile_id"] == "anbernic"

    def test_subsequent_calls_reuse_the_same_destination(
        self, qapp, seeded_db, monkeypatch, tmp_path
    ) -> None:
        """The second one-shot scan for the same path doesn't insert a duplicate."""
        from romulus.models.profile import DestinationProfile
        from romulus.ui.main_window import MainWindow

        window = MainWindow(seeded_db)
        window._export_dialog = None  # type: ignore[assignment]

        captured_ids: list[int] = []

        class _FakeSignal:
            def connect(self, *_args, **_kwargs) -> None:
                pass

        class _FakeWorker:
            def __init__(
                self, _db_path, dest_id: int, _target: str, **_kw
            ) -> None:
                captured_ids.append(dest_id)
                self.progress = _FakeSignal()
                self.finished_ok = _FakeSignal()
                self.failed = _FakeSignal()
                self.finished = _FakeSignal()

            def start(self) -> None:
                pass

            def cancel(self) -> None:  # pragma: no cover
                pass

            def isRunning(self) -> bool:  # noqa: N802
                return False

            def deleteLater(self) -> None:  # noqa: N802
                pass

        monkeypatch.setattr(
            "romulus.ui.main_window.DestInventoryWorker", _FakeWorker
        )
        monkeypatch.setattr(
            "romulus.ui.main_window.DestScanProgressDialog.show",
            lambda self: None,
        )

        target_path = str(tmp_path / "dest")
        profile = DestinationProfile(
            id="anbernic",
            name="Anbernic",
            base_path="Roms",
            gamelist_format="emulationstation_xml",
            artwork_subdir=None,
            artwork_filename_template="{stem}{ext}",
            multi_disc=None,
            systems={},
        )

        window._on_sync_scan_requested(profile, target_path, "push_merge", False, -1)
        # Clear the running-worker guard so the second call proceeds.
        window._dest_inventory_worker = None  # type: ignore[assignment]
        window._on_sync_scan_requested(profile, target_path, "push_merge", False, -1)

        assert captured_ids[0] == captured_ids[1]
        # Only one row in the DB even after two one-shot scans.
        assert len(q.get_sync_destinations(seeded_db)) == 1


# ---------------------------------------------------------------------------
# Bug 2 — DestScanProgressDialog wiring
# ---------------------------------------------------------------------------


class TestDestScanProgressDialog:
    def test_opens_with_indeterminate_bar(self, qapp) -> None:
        dialog = DestScanProgressDialog()
        # Indeterminate when max == 0 (the QProgressDialog convention).
        assert dialog.maximum() == 0
        assert dialog.minimum() == 0

    def test_on_progress_updates_determinate_bar(self, qapp) -> None:
        dialog = DestScanProgressDialog()
        dialog.on_progress(5, 100, "gba/Game.gba")
        assert dialog.maximum() == 100
        assert dialog.value() == 5
        # Label shows path + count.
        assert "5 of 100" in dialog.labelText()
        assert "Game.gba" in dialog.labelText()

    def test_on_progress_stays_indeterminate_when_total_zero(self, qapp) -> None:
        dialog = DestScanProgressDialog()
        dialog.on_progress(0, 0, "discovering...")
        # Still indeterminate.
        assert dialog.maximum() == 0
        assert "Walking destination" in dialog.labelText()

    def test_on_finished_flips_to_close_state(self, qapp) -> None:
        dialog = DestScanProgressDialog()
        dialog.on_finished(object())
        assert dialog.maximum() == 1
        assert dialog.value() == 1
        # Button text switches to Close so the user can dismiss the dialog.
        assert dialog.labelText().startswith("✓")

    def test_on_failed_sets_error_label(self, qapp) -> None:
        dialog = DestScanProgressDialog()
        dialog.on_failed("Destination scan cancelled")
        assert "cancelled" in dialog.labelText().lower()


# ---------------------------------------------------------------------------
# Bug 3 — SyncPreviewDialog intro + Apply button label
# ---------------------------------------------------------------------------


def _add_action(rel: str = "snes/A.sfc", size: int = 1024) -> SyncAction:
    return SyncAction(
        kind=ACTION_COPY_TO_DEST,
        rel_path=rel,
        local_path=f"/lib/{rel}",
        dest_path=f"/dest/{rel}",
        size_bytes=size,
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
        system_id="snes",
    )


def _plan_with(actions: list[SyncAction]) -> SyncPlan:
    return SyncPlan(dest_id=1, mode="push_merge", actions=actions, conflict_policy="skip")  # type: ignore[arg-type]


class TestSyncPreviewIntroAndButton:
    def test_intro_paragraph_is_visible(self, qapp) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/anbernic")
        assert dialog._intro_label.text() != ""
        assert "diff" in dialog._intro_label.text().lower()
        assert "apply" in dialog._intro_label.text().lower()

    def test_apply_button_includes_target_path(self, qapp) -> None:
        target = "/mnt/anbernic"
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label=target)
        assert "Apply changes to" in dialog._apply_btn.text()
        assert target in dialog._apply_btn.text()

    def test_apply_button_truncates_long_target(self, qapp) -> None:
        target = "/very/long/network/path/" + ("x" * 200) + "/sdcard"
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label=target)
        # The total button label should stay under 80 chars regardless of
        # how long the network path is.
        assert len(dialog._apply_btn.text()) < 90
        # Trailing path component still visible (the disambiguator).
        assert "sdcard" in dialog._apply_btn.text()

    def test_cancel_button_has_explanatory_tooltip(self, qapp) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")
        assert dialog._cancel_btn is not None
        tip = dialog._cancel_btn.toolTip().lower()
        assert "discard" in tip or "without writing" in tip

    def test_totals_label_shows_copy_and_delete_counts(self, qapp) -> None:
        plan = _plan_with(
            [
                _add_action("snes/A.sfc", size=1024 * 1024),
                _add_action("snes/B.sfc", size=2 * 1024 * 1024),
                _delete_action("snes/Orphan.sfc"),
            ]
        )
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")
        text = dialog._totals_label.text()
        assert "2 file(s)" in text  # copies
        assert "1 file(s) to delete" in text


# ---------------------------------------------------------------------------
# Bug 3 — Non-destructive Apply preserves existing single-click behaviour
# ---------------------------------------------------------------------------


class TestNonDestructiveApply:
    def test_non_destructive_plan_emits_actions_without_extra_prompts(
        self, qapp
    ) -> None:
        """Pure-add plans must still apply with a single click."""
        plan = _plan_with(
            [_add_action("snes/A.sfc"), _add_action("snes/B.sfc")]
        )
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")
        received: list[list[SyncAction]] = []
        dialog.actions_approved.connect(lambda approved: received.append(approved))
        with patch.object(QMessageBox, "question") as mock_q:
            dialog._on_apply_clicked()
        # Destructive prompts never fired.
        assert mock_q.call_count == 0
        # Both actions ended up in the emitted approved list.
        assert len(received) == 1
        assert {a.rel_path for a in received[0]} == {
            "snes/A.sfc",
            "snes/B.sfc",
        }


# ---------------------------------------------------------------------------
# Bug 3 — Round-trip integration: preview -> Apply -> all checked actions emitted
# ---------------------------------------------------------------------------


class TestPreviewApplyRoundtrip:
    def test_3_action_plan_passes_all_3_actions_through(self, qapp) -> None:
        """Click Apply with default checks — all 3 actions are emitted."""
        actions = [
            _add_action("snes/A.sfc", size=100),
            _add_action("snes/B.sfc", size=200),
            _add_action("snes/C.sfc", size=300),
        ]
        plan = _plan_with(actions)
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")

        captured: list[list[SyncAction]] = []
        dialog.actions_approved.connect(lambda approved: captured.append(approved))
        # No destructive prompts to mock (pure adds).
        dialog._on_apply_clicked()

        assert len(captured) == 1
        emitted = captured[0]
        assert len(emitted) == 3
        rel_paths = {a.rel_path for a in emitted}
        assert rel_paths == {"snes/A.sfc", "snes/B.sfc", "snes/C.sfc"}


class TestSyncPreviewDoneState:
    """After apply finishes (success or failure), the dialog must show a
    single 'Close' button rather than 'Apply (disabled) / Cancel'. The user
    was clicking Cancel after a completed sync and reading it as 'cancel
    what?'.
    """

    def test_on_finished_hides_apply_and_renames_cancel_to_close(
        self, qapp
    ) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")
        assert dialog._apply_btn.isVisible() or not dialog.isVisible()
        dialog.on_finished(applied=1, skipped=0, failed=0)
        # Apply hidden; Cancel renamed to Close and re-wired to accept().
        assert not dialog._apply_btn.isVisible()
        assert dialog._cancel_btn is not None
        assert dialog._cancel_btn.text() == "Close"

    def test_on_failed_hides_apply_and_renames_cancel_to_close(
        self, qapp
    ) -> None:
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")
        dialog.on_failed("simulated failure")
        assert not dialog._apply_btn.isVisible()
        assert dialog._cancel_btn is not None
        assert dialog._cancel_btn.text() == "Close"

    def test_done_state_is_idempotent(self, qapp) -> None:
        """Multiple finished/failed callbacks (defensive) don't break the UI."""
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")
        dialog.on_finished(applied=1, skipped=0, failed=0)
        dialog.on_finished(applied=1, skipped=0, failed=0)  # double-call
        dialog.on_failed("late failure")  # mixed-call after finished
        assert dialog._cancel_btn.text() == "Close"

    def test_close_button_triggers_accept_not_reject(self, qapp) -> None:
        """In done-state the Close button maps to accept() so callers wrapping
        exec() see ``QDialog.Accepted``, not ``Rejected`` (which would mean
        'plan was cancelled')."""
        plan = _plan_with([_add_action()])
        dialog = SyncPreviewDialog(plan, destination_label="/mnt/x")
        dialog.on_finished(applied=1, skipped=0, failed=0)
        results: list[int] = []
        dialog.accepted.connect(lambda: results.append(1))
        dialog.rejected.connect(lambda: results.append(0))
        dialog._cancel_btn.click()
        assert results == [1]
