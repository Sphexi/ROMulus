"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os

import pytest

from romulus.db import create_tables
from romulus.db.connection import get_connection
from romulus.models import seed_systems

# Force Qt to use the offscreen platform plugin in tests so headless CI works.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def db(tmp_path):
    """A fully-initialized SQLite connection backed by a temp file.

    Reuses ``get_connection`` so tests pick up future connection-setup tweaks
    (PRAGMA additions, etc.) automatically. ``_restrict_db_permissions`` is a
    no-op on Windows and silently passes through OSError elsewhere, so it is
    safe to call here.
    """
    conn = get_connection(tmp_path / "test.db")
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
