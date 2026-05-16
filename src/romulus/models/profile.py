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

Security: ``base_path`` and ``folder`` are user-supplied (community profiles
under ``~/.romulus/profiles/``). Pydantic validators reject absolute paths,
``..`` segments, drive letters, and Windows reserved names so a malicious
profile YAML can't escape the export target directory. See security audit
v0.1.0 finding #1.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field, field_validator

# Windows reserved device names — case-insensitive. A path stem matching any
# of these (with or without extension) is rejected because Windows reroutes
# the I/O to a device driver instead of the disk.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "con", "prn", "aux", "nul",
        "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    }
)


def _validate_relative_path(value: str, *, field_name: str, allow_empty: bool) -> str:
    """Reject path values that could escape the export target.

    Permits empty / forward-slash subpaths like ``roms`` or ``nes/common``.
    Rejects absolute paths (``/etc`` or ``C:\\Users``), ``..`` traversal
    segments, drive-letter prefixes, backslash separators (folded into
    forward slashes is the YAML convention; literal backslashes in YAML are
    treated as suspicious here), and Windows reserved device names.
    """
    if not value:
        if allow_empty:
            return value
        raise ValueError(f"{field_name} must not be empty")

    if "\x00" in value:
        raise ValueError(f"{field_name} contains NUL byte: {value!r}")

    # Reject Windows drive-letter prefixes (``C:foo`` or ``C:\foo``). The colon
    # is also illegal in path segments on Windows, so a blanket "no ``:``" rule
    # is correct.
    if ":" in value:
        raise ValueError(f"{field_name} must not contain ':' (got {value!r})")

    # POSIX absolute (``/etc``) and Windows-UNC (``\\server\share``) prefixes.
    if value.startswith(("/", "\\")):
        raise ValueError(f"{field_name} must be a relative path (got {value!r})")

    # Normalize to forward slashes for segment inspection. We also forbid
    # backslashes in the raw value above this normalization for consistency
    # with the relative-path / absolute-path checks; but a value like
    # ``nes\common`` is ambiguous on Windows vs. POSIX, so we treat any
    # backslash anywhere as a hard reject.
    if "\\" in value:
        raise ValueError(
            f"{field_name} must use '/' as the separator (got {value!r})"
        )

    segments = [seg for seg in value.split("/") if seg]
    if not segments and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")

    for seg in segments:
        if seg == "..":
            raise ValueError(
                f"{field_name} must not contain '..' segments (got {value!r})"
            )
        # Strip trailing dots/spaces — Windows silently mangles those.
        stripped = seg.rstrip(". ")
        stem = stripped.split(".", 1)[0].lower()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError(
                f"{field_name} contains Windows reserved name {seg!r} "
                f"(in {value!r})"
            )
        # Reject ASCII control characters.
        if any(ord(c) < 0x20 for c in seg):
            raise ValueError(
                f"{field_name} contains control character (got {value!r})"
            )

    return value


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

    @field_validator("folder")
    @classmethod
    def _check_folder(cls, value: str) -> str:
        """Reject path-traversal / absolute / reserved-name folder values."""
        return _validate_relative_path(value, field_name="folder", allow_empty=True)

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
    #: Filename template for the copied artwork file. ``{stem}`` is the ROM's
    #: filename without extension; ``{ext}`` is the source image's extension
    #: (including the leading dot). EmulationStation-classic targets
    #: (Batocera, RetroPie) want ``"{stem}-image{ext}"``; modern launchers
    #: (Daijisho, Onion, muOS, ES-DE) want ``"{stem}{ext}"`` — the cleaner
    #: default.
    artwork_filename_template: str = "{stem}{ext}"
    multi_disc: str | None = None
    systems: dict[str, SystemMapping] = Field(default_factory=dict)

    @field_validator("base_path")
    @classmethod
    def _check_base_path(cls, value: str) -> str:
        """Reject path-traversal / absolute / reserved-name base_path values."""
        return _validate_relative_path(value, field_name="base_path", allow_empty=False)

    @field_validator("artwork_subdir")
    @classmethod
    def _check_artwork_subdir(cls, value: str | None) -> str | None:
        """Reject path-traversal / absolute artwork_subdir values."""
        if value is None:
            return None
        return _validate_relative_path(
            value, field_name="artwork_subdir", allow_empty=True
        )
