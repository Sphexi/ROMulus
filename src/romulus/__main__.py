"""ROMulus — entry point."""

from __future__ import annotations

import sys


def main() -> None:
    """Launch the ROMulus desktop application."""
    from romulus.app import run

    sys.exit(run())


if __name__ == "__main__":
    main()
