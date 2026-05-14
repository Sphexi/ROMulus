"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import sqlite3

import pytest

from romulus.db import create_tables
from romulus.models import seed_systems


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
