"""TypedDict shapes shared across metadata providers.

The Hasheous, LaunchBox, and ScreenScraper clients all produce the same
fixed-shape mapping of metadata fields. Declaring it once here gives every
provider, the DB layer's ``upsert_metadata``, and the orchestrator in
``metadata/__init__`` a single contract to honour — and gives type checkers
something better than ``dict[str, Any]`` to reason about.
"""

from __future__ import annotations

from typing import TypedDict


class MetadataPayload(TypedDict, total=False):
    """Per-game metadata as returned by every provider in this package."""

    title: str | None
    description: str | None
    genre: str | None
    developer: str | None
    publisher: str | None
    release_date: str | None
    players: str | None
    rating: str | None
