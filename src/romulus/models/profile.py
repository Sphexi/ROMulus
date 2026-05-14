"""Destination profile data model."""

from __future__ import annotations

from pydantic import BaseModel


class DestinationProfile(BaseModel):
    """An export target profile (Anbernic, Batocera, MiSTer, etc.).

    Profiles are loaded from YAML files in `data/profiles/` (built-in) or
    `~/.romulus/profiles/` (user). They describe the on-device folder layout
    and gamelist format for a particular handheld/launcher.
    """

    id: str
    name: str
    base_path: str
    gamelist_format: str
    systems: dict[str, str]
