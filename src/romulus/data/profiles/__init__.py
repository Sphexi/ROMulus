"""Compatibility shim for the historical bundled-profile location.

v0.1.0 shipped destination-profile YAMLs inside the wheel at
``src/romulus/data/profiles/``. v0.2.0 moves them out to a top-level
``profiles/`` directory at the install root so end users can edit them
without digging into the package. This subpackage is kept as an importable
module only because :mod:`romulus.core.exporter` historically resolved the
built-in directory via :mod:`importlib.resources` against it. The exporter
now falls back to the in-repo / install-root ``profiles/`` directory first
and only consults this package as a last resort.
"""
