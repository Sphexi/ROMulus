"""Romulus — entry point."""

from romulus import __version__


def main() -> None:
    """Print the Romulus version banner.

    Placeholder entry point used until the PySide6 main window is wired up
    in a later session.
    """
    print(f"Romulus v{__version__}")


if __name__ == "__main__":
    main()
