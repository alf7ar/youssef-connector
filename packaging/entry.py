"""PyInstaller entry point (keeps the `connector` package imports relative)."""
import sys

from connector.main import main

if __name__ == "__main__":
    sys.exit(main())
