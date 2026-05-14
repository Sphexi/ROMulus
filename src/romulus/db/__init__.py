"""Database layer — SQLite connection, schema, and queries."""

from romulus.db.config import (
    DEFAULT_CONFIG,
    get_all_config,
    get_config,
    seed_defaults,
    set_config,
)
from romulus.db.connection import DEFAULT_DB_PATH, get_connection
from romulus.db.schema import create_tables

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_DB_PATH",
    "create_tables",
    "get_all_config",
    "get_config",
    "get_connection",
    "seed_defaults",
    "set_config",
]
