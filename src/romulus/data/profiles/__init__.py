"""Bundled destination profile YAML files (one per supported target device).

This subpackage is intentionally code-free — its sole purpose is to mark
``romulus/data/profiles/*.yaml`` as package data so ``importlib.resources``
can locate the YAML files reliably regardless of how Romulus was installed
(``pip install .``, editable install, or running from source).
"""
