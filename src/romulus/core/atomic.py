"""Atomic filesystem helpers shared across the core engine.

Both the organizer (Session 9) and the exporter (Session 10) need to write or
move files in a way that never leaves a half-written artifact at the final
path. The reference pattern was first established in
``romulus.metadata.libretro.fetch_cover`` (Session 6 / Session 8): stage the
output to a sibling ``tempfile.mkstemp`` in the destination directory, then
``os.replace`` it into place. ``os.replace`` is atomic on a single filesystem,
so a crash or cancel in the middle of the copy can only leave a leftover
``.part`` tempfile — never a corrupted target.

This module factors that pattern out so the organizer and exporter share a
single implementation. New code that needs to publish a file to a final path
should use ``atomic_replace`` (move an existing file) or ``atomic_write_bytes``
(write fresh bytes) rather than re-implementing the dance.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Streaming copy chunk size — matches the value the organizer's pre-factor
# implementation used so behaviour stays identical for callers that migrated.
_COPY_CHUNK_BYTES = 1024 * 1024


def atomic_replace(source: Path, dest: Path) -> None:
    """Move ``source`` to ``dest`` atomically.

    For same-filesystem renames this is a single ``os.replace``. For
    cross-filesystem moves we stream the file into a tempfile sibling of the
    destination (``tempfile.mkstemp`` in ``dest.parent``) and then
    ``os.replace`` into place — same pattern as
    ``romulus.metadata.libretro.fetch_cover`` so a crash mid-copy can never
    leave a half-written file at the final path.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, dest)
        return
    except OSError as exc:
        # EXDEV / cross-device — fall through to stream-via-tempfile path.
        logger.debug("atomic rename fell back to copy: src=%s err=%s", source, exc)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".part", dir=str(dest.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_fh, source.open("rb") as src_fh:
            while True:
                chunk = src_fh.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                tmp_fh.write(chunk)
        os.replace(tmp_path, dest)
    except OSError:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise
    try:
        source.unlink()
    except OSError as exc:
        logger.warning(
            "atomic move: dest write OK but source unlink failed: src=%s err=%s",
            source,
            exc,
        )


def atomic_copy(source: Path, dest: Path) -> None:
    """Copy ``source`` to ``dest`` atomically, leaving ``source`` in place.

    Streams the bytes into a sibling ``tempfile.mkstemp`` in ``dest.parent``
    and ``os.replace``s it into place. A failure mid-copy unlinks the temp
    file and propagates the ``OSError``; the destination either does not exist
    or contains the fully-written bytes — never anything partial.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".part", dir=str(dest.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_fh, source.open("rb") as src_fh:
            shutil.copyfileobj(src_fh, tmp_fh, length=_COPY_CHUNK_BYTES)
        os.replace(tmp_path, dest)
    except OSError:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def atomic_write_bytes(payload: bytes, dest: Path) -> None:
    """Write ``payload`` to ``dest`` atomically.

    Used for generated artifacts (gamelist.xml, .m3u playlists) where there is
    no source file on disk. Same staging dance: write to a sibling tempfile
    then ``os.replace`` into place.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".part", dir=str(dest.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_fh:
            tmp_fh.write(payload)
        os.replace(tmp_path, dest)
    except OSError:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def atomic_write_text(text: str, dest: Path, encoding: str = "utf-8") -> None:
    """Convenience wrapper: encode ``text`` and write it via ``atomic_write_bytes``."""
    atomic_write_bytes(text.encode(encoding), dest)
