"""Frozen-executable entry point (PyInstaller cannot run a package's relative-import __main__)."""
import sys

from runpeek.cli import main

if __name__ == "__main__":
    sys.exit(main())
