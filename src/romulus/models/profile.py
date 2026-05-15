"""Destination profile data model.

Profiles describe how Romulus should lay out an exported ROM collection on a
target device (Batocera, RetroPie, Onion OS, muOS, MiSTer, Analogue Pocket,
etc.). The on-disk format is YAML; this module is the typed in-memory
representation loaded by the exporter.

The session-10 carry-forward rule is strict: every built-in profile must list
a folder mapping for every system in the registry. Systems the target does
NOT support are still listed, just with ``supported: false`` (or an empty
``folder``). The exporter uses ``SystemMapping.is_supported`` to skip those
explicitly rather than silently dropping them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field


class SystemMapping(BaseModel):
    """How one system is rendered inside a destination profile.

    A mapping with ``supported=False`` (or an empty ``folder``) means the
    target device cannot run this system — the exporter must skip it
    explicitly rather than silently omitting it. This lets the test suite
    assert that every built-in profile makes an explicit decision for every
    system in :data:`romulus.models.system.SYSTEM_REGISTRY`.
    """

    folder: str = ""
    extensions: list[str] = Field(default_factory=list)
    supported: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_supported(self) -> bool:
        """True only if the target supports this system AND a folder is set."""
        return self.supported and bool(self.folder)


class DestinationProfile(BaseModel):
    """An export target profile (Batocera, MiSTer, Anbernic, etc.).

    Profiles are loaded from YAML files in
    :data:`romulus.core.exporter.BUILTIN_PROFILES_DIR` (bundled inside the
    package via ``importlib.resources``) or ``~/.romulus/profiles/`` (user).
    They describe the on-device folder layout and gamelist format for a
    particular handheld/launcher.

    ``systems`` is keyed by the system id from
    :data:`romulus.models.system.SYSTEM_REGISTRY`. Every registry entry must
    appear in every built-in profile's ``systems`` map — see
    :class:`SystemMapping` for the unsupported-system convention.
    """

    id: str
    name: str
    description: str | None = None
    case_sensitive: bool = True
    base_path: str
    gamelist_format: str | None = None
    artwork_subdir: str | None = None
    multi_disc: str | None = None
    systems: dict[str, SystemMapping] = Field(default_factory=dict)
