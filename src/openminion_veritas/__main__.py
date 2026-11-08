"""Module entry point for `python -m openminion_veritas`."""

from __future__ import annotations

import sys

from openminion_veritas.cli import main

if __name__ == "__main__":
    sys.exit(main())
