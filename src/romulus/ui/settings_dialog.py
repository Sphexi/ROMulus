"""Settings dialog — library path, DATs, metadata, scan config."""

from __future__ import annotations

import json
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from romulus.app import DEFAULT_LOG_PATH, set_log_level
from romulus.db import get_config, set_config
from romulus.metadata.screenscraper import test_connection as screenscraper_test_connection


class _GeneralTab(QWidget):
    """Library path + theme selector."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

        self.library_path = QLineEdit(get_config(conn, "library_path") or "")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._pick_folder)
        path_row = QHBoxLayout()
        path_row.addWidget(self.library_path)
        path_row.addWidget(browse)

        self.theme = QComboBox()
        self.theme.addItems(["system", "light", "dark"])
        current_theme = get_config(conn, "theme") or "system"
        idx = max(0, self.theme.findText(current_theme))
        self.theme.setCurrentIndex(idx)

        form = QFormLayout(self)
        form.addRow("Library path:", path_row)
        form.addRow("Theme:", self.theme)

    def _pick_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select ROM library folder", self.library_path.text() or ""
        )
        if chosen:
            self.library_path.setText(chosen)

    def save(self) -> None:
        set_config(self._conn, "library_path", self.library_path.text())
        set_config(self._conn, "theme", self.theme.currentText())


class _DatTab(QWidget):
    """DAT folder paths — list + add/remove."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

        raw = get_config(conn, "dat_paths") or "[]"
        try:
            paths = json.loads(raw)
            if not isinstance(paths, list):
                paths = []
        except (ValueError, TypeError):
            paths = []

        self.list = QListWidget()
        for path in paths:
            self.list.addItem(str(path))

        add_btn = QPushButton("Add folder...")
        add_btn.clicked.connect(self._add)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove)

        buttons = QHBoxLayout()
        buttons.addWidget(add_btn)
        buttons.addWidget(remove_btn)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("DAT folders scanned for No-Intro / Redump XML files:"))
        layout.addWidget(self.list)
        layout.addLayout(buttons)

    def _add(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Add DAT folder", "")
        if chosen:
            self.list.addItem(chosen)

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def save(self) -> None:
        paths = [self.list.item(i).text() for i in range(self.list.count())]
        set_config(self._conn, "dat_paths", json.dumps(paths))


class _MetadataTab(QWidget):
    """ScreenScraper credentials."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self.username = QLineEdit(get_config(conn, "screenscraper_username") or "")
        self.password = QLineEdit(get_config(conn, "screenscraper_password") or "")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.test_button = QPushButton("Test connection")
        self.test_button.setToolTip(
            "Validate the current username/password against ScreenScraper."
        )
        self.test_button.clicked.connect(self._on_test_connection)

        form = QFormLayout(self)
        form.addRow("ScreenScraper username:", self.username)
        form.addRow("ScreenScraper password:", self.password)
        form.addRow("", self.test_button)

    def _on_test_connection(self) -> None:
        """Validate the current form values via ScreenScraper's user-info API.

        Uses the current widget values rather than the saved config so the user
        can validate credentials before clicking OK on the dialog. The button
        is disabled while the request is in flight so multiple clicks can't
        stack network requests.
        """
        self.test_button.setEnabled(False)
        self.test_button.setText("Testing...")
        try:
            ok, message = screenscraper_test_connection(
                self.username.text().strip(),
                self.password.text(),
            )
        finally:
            self.test_button.setText("Test connection")
            self.test_button.setEnabled(True)

        if ok:
            QMessageBox.information(self, "ScreenScraper", message)
        else:
            QMessageBox.warning(self, "ScreenScraper", message)

    def save(self) -> None:
        set_config(self._conn, "screenscraper_username", self.username.text())
        set_config(self._conn, "screenscraper_password", self.password.text())


class _ScanTab(QWidget):
    """Scan thread count."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self.threads = QSpinBox()
        self.threads.setRange(1, 64)
        try:
            self.threads.setValue(int(get_config(conn, "scan_threads") or "8"))
        except (TypeError, ValueError):
            self.threads.setValue(8)

        form = QFormLayout(self)
        form.addRow("Hash threads:", self.threads)

    def save(self) -> None:
        set_config(self._conn, "scan_threads", str(self.threads.value()))


class _DiagnosticsTab(QWidget):
    """Log level + log file location."""

    LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

        self.level = QComboBox()
        self.level.addItems(self.LEVELS)
        current = (get_config(conn, "log_level") or "INFO").upper()
        idx = max(0, self.level.findText(current))
        self.level.setCurrentIndex(idx)

        log_path_label = QLabel(str(DEFAULT_LOG_PATH))
        log_path_label.setTextInteractionFlags(
            log_path_label.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        log_path_label.setToolTip("Click to select; copy with Ctrl+C.")

        form = QFormLayout(self)
        form.addRow("Log level:", self.level)
        form.addRow("Log file:", log_path_label)

    def save(self) -> None:
        chosen = self.level.currentText()
        set_config(self._conn, "log_level", chosen)
        # Apply immediately so the user sees the new level take effect
        # without needing to restart the app.
        set_log_level(chosen)


class SettingsDialog(QDialog):
    """Top-level settings dialog with four tabs, writes back to the config table."""

    def __init__(self, conn: sqlite3.Connection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Romulus Settings")
        self._conn = conn

        self.general = _GeneralTab(conn)
        self.dats = _DatTab(conn)
        self.metadata = _MetadataTab(conn)
        self.scan = _ScanTab(conn)
        self.diagnostics = _DiagnosticsTab(conn)

        tabs = QTabWidget()
        tabs.addTab(self.general, "General")
        tabs.addTab(self.dats, "DATs")
        tabs.addTab(self.metadata, "Metadata")
        tabs.addTab(self.scan, "Scan")
        tabs.addTab(self.diagnostics, "Diagnostics")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_and_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def _accept_and_save(self) -> None:
        self.general.save()
        self.dats.save()
        self.metadata.save()
        self.scan.save()
        self.diagnostics.save()
        self.accept()
