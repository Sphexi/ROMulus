"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os
import sqlite3

import pytest

from romulus.db import create_tables
from romulus.models import seed_systems

# Force Qt to use the offscreen platform plugin in tests so headless CI works.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def db(tmp_path):
    """A fully-initialized SQLite connection backed by a temp file.

    Schema is created but no rows are seeded — tests decide what to seed.
    Using a file (not :memory:) so multiple cursors and WAL mode behave the
    same way as in production.
    """
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    create_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def seeded_db(db):
    """A SQLite connection with schema created AND the system registry seeded."""
    seed_systems(db)
    return db


@pytest.fixture(scope="session")
def qapp():
    """Session-wide QApplication so widgets can be instantiated headlessly."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
