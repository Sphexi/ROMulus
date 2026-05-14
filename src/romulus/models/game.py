"""Logical game data model."""

from __future__ import annotations

from pydantic import BaseModel


class Game(BaseModel):
    """A logical game — one entry per title per system, grouping multiple ROMs.

    A single game may have many ROM files (different regions, revisions, formats).
    Hacks and homebrew are first-class artifacts: they get their own Game records
    rather than being collapsed against the official release.
    """

    id: int | None = None
    title: str
    system_id: str
    canonical_name: str | None = None
    region: str | None = None
    revision: str | None = None
    is_hack: bool = False
    is_homebrew: bool = False
    is_bios: bool = False
