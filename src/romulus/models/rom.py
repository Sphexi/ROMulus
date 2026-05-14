"""ROM file data model."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RomFile(BaseModel):
    """A single ROM file on disk, as discovered by the scanner.

    Mirrors the `roms` table schema. The `id`, `game_id`, and `scan_id` fields
    are assigned by the database; everything else is populated by the scanner
    (Layer 1+2) and later enriched by the identifier pipeline (Layer 3).
    """

    id: int | None = None
    path: str
    filename: str
    extension: str = Field(..., description="Lowercase, includes leading dot, e.g. '.sfc'")
    size_bytes: int = Field(..., ge=0)
    mtime: float
    system_id: str | None = None
    game_id: int | None = None
    scan_id: int | None = None
    fuzzy_key: str | None = None
    header_title: str | None = None
    dat_match: str | None = None
    match_confidence: str = Field(
        default="unmatched",
        description="One of: unmatched | fuzzy | header | dat_verified",
    )
