"""Smoke + behaviour tests for the per-system summary dialog.

The dialog is generic — fed a column spec + a rows-by-system dict —
with two factory methods, one each for Export and Sync summaries.
These tests exercise:

* The factory methods read the right fields off the engine summaries.
* The table renders one row per system + a totals row.
* The totals row matches the sum of every per-system value.
* Empty per-system dicts don't crash (zero-action runs).
"""

from __future__ import annotations

from PySide6.QtWidgets import QTableWidget

from romulus.core.exporter import ExportSummary, PerSystemExportCounts
from romulus.core.sync import PerSystemSyncCounts, SyncSummary
from romulus.ui.per_system_summary_dialog import PerSystemSummaryDialog


class TestExportSummaryDialog:
    def test_renders_one_row_per_system_plus_totals(self, qapp) -> None:
        summary = ExportSummary(
            files_copied=12,
            files_skipped=8,
            bytes_copied=2048,
            systems=["snes", "mame"],
            errors=["refusing to overwrite x"],
            per_system={
                "snes": PerSystemExportCounts(copied=10, bytes_copied=1024),
                "mame": PerSystemExportCounts(
                    copied=2,
                    bytes_copied=1024,
                    skipped_refused=1,
                    errors=1,
                ),
                "amiga": PerSystemExportCounts(skipped_unsupported=7),
            },
        )
        dialog = PerSystemSummaryDialog.for_export(summary)
        try:
            # 3 systems + 1 totals row.
            table = dialog.findChild(QTableWidget)
            assert table is not None
            assert table.rowCount() == 4
            # Header: System + 7 export columns
            # (Copied | Bytes | Covers refreshed | Already | Unsupported | Refused | Errors).
            assert table.columnCount() == 8
        finally:
            dialog.close()

    def test_totals_row_sums_per_system_counts(self, qapp) -> None:
        summary = ExportSummary(
            files_copied=12,
            files_skipped=8,
            bytes_copied=2048,
            systems=["snes", "mame", "amiga"],
            per_system={
                "snes": PerSystemExportCounts(copied=10, bytes_copied=1024),
                "mame": PerSystemExportCounts(
                    copied=2,
                    bytes_copied=1024,
                    skipped_refused=1,
                    errors=1,
                ),
                "amiga": PerSystemExportCounts(skipped_unsupported=7),
            },
        )
        dialog = PerSystemSummaryDialog.for_export(summary)
        try:
            table = dialog.findChild(QTableWidget)
            assert table is not None
            totals_row = table.rowCount() - 1
            assert table.item(totals_row, 0).text() == "Totals"
            # Column order: System | Copied | Bytes | Covers refreshed
            # | Already | Unsupported | Refused | Errors.
            assert table.item(totals_row, 1).text() == "12"  # copied
            assert table.item(totals_row, 3).text() == "0"   # covers refreshed
            assert table.item(totals_row, 5).text() == "7"   # unsupported
            assert table.item(totals_row, 6).text() == "1"   # refused
            assert table.item(totals_row, 7).text() == "1"   # errors
        finally:
            dialog.close()

    def test_artwork_only_run_surfaces_covers_refreshed(self, qapp) -> None:
        """In artwork-only mode every ROM-centric counter is 0. The
        Covers refreshed column has to carry the entire story —
        without it the dialog would show empty rows and the user
        wouldn't see what actually happened.
        """
        summary = ExportSummary(
            files_copied=0,
            files_skipped=0,
            bytes_copied=0,
            systems=["snes", "nes"],
            artwork_copied=42,
            gamelists_written=2,
            per_system={
                "snes": PerSystemExportCounts(artwork_copied=30),
                "nes": PerSystemExportCounts(artwork_copied=12),
            },
        )
        dialog = PerSystemSummaryDialog.for_export(summary)
        try:
            table = dialog.findChild(QTableWidget)
            assert table is not None
            totals_row = table.rowCount() - 1
            # Covers refreshed totals to 42; ROM-centric columns all 0.
            assert table.item(totals_row, 3).text() == "42"
            assert table.item(totals_row, 1).text() == "0"   # copied
            assert table.item(totals_row, 2).text() == "0"   # bytes (0 renders as "0")
        finally:
            dialog.close()

    def test_empty_per_system_does_not_crash(self, qapp) -> None:
        """A clean no-op export (every file already on dest) should still render."""
        summary = ExportSummary()
        dialog = PerSystemSummaryDialog.for_export(summary)
        try:
            table = dialog.findChild(QTableWidget)
            assert table is not None
            # No per-system data — table has just the totals row.
            assert table.rowCount() == 1
            assert table.item(0, 0).text() == "Totals"
        finally:
            dialog.close()

    def test_systems_listed_even_if_only_skipped(self, qapp) -> None:
        """A system that was entirely skipped (e.g. unsupported) still
        gets a row in the table — the user needs to see "we received
        4,363 amiga files and skipped all of them" to understand the
        result.
        """
        summary = ExportSummary(
            files_skipped=4363,
            per_system={
                "amiga": PerSystemExportCounts(skipped_unsupported=4363),
            },
        )
        dialog = PerSystemSummaryDialog.for_export(summary)
        try:
            table = dialog.findChild(QTableWidget)
            assert table is not None
            assert table.rowCount() == 2  # amiga + totals
            assert table.item(0, 0).text() == "amiga"
        finally:
            dialog.close()


class TestSyncSummaryDialog:
    def test_renders_sync_columns(self, qapp) -> None:
        summary = SyncSummary(
            applied=5,
            skipped=2,
            failed=0,
            systems_touched={"snes"},
            per_system={
                "snes": PerSystemSyncCounts(
                    copied_to_dest=3,
                    copied_to_local=2,
                    deleted_dest=1,
                    skipped_identical=2,
                    bytes_copied=4096,
                ),
            },
        )
        dialog = PerSystemSummaryDialog.for_sync(summary)
        try:
            table = dialog.findChild(QTableWidget)
            assert table is not None
            # Sync column spec: 7 numeric/bytes columns + System label.
            assert table.columnCount() == 8
            # snes row + totals.
            assert table.rowCount() == 2
        finally:
            dialog.close()

    def test_failed_actions_surface_in_totals(self, qapp) -> None:
        summary = SyncSummary(
            failed=2,
            systems_touched={"snes", "psx"},
            per_system={
                "snes": PerSystemSyncCounts(failed=1),
                "psx": PerSystemSyncCounts(failed=1),
            },
        )
        dialog = PerSystemSummaryDialog.for_sync(summary)
        try:
            table = dialog.findChild(QTableWidget)
            assert table is not None
            totals_row = table.rowCount() - 1
            # "Failed" is the last sync column (index 7 from System=0).
            assert table.item(totals_row, 7).text() == "2"
        finally:
            dialog.close()
