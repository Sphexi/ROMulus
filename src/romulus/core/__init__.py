"""Core engine — scanner, identifier, hasher, organizer, exporter."""

from romulus.core.dat_parser import (
    DatEntry,
    load_all_dats,
    match_hashes,
    parse_dat_file,
    parse_region_from_name,
)
from romulus.core.hasher import (
    HashResult,
    hash_library,
    hash_rom,
    normalize_rom_content,
)
from romulus.core.identifier import extract_header_title
from romulus.core.scanner import (
    SIDE_FILE_EXTENSIONS,
    ParsedFilename,
    ScanResult,
    detect_system,
    generate_fuzzy_key,
    group_into_games,
    is_rom_file,
    is_side_file,
    parse_filename,
    scan_library,
)

__all__ = [
    "SIDE_FILE_EXTENSIONS",
    "DatEntry",
    "HashResult",
    "ParsedFilename",
    "ScanResult",
    "detect_system",
    "extract_header_title",
    "generate_fuzzy_key",
    "group_into_games",
    "hash_library",
    "hash_rom",
    "is_rom_file",
    "is_side_file",
    "load_all_dats",
    "match_hashes",
    "normalize_rom_content",
    "parse_dat_file",
    "parse_filename",
    "parse_region_from_name",
    "scan_library",
]
