"""Alternate entry point for macOS/Linux (python3 main.py)."""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().with_name("main.pyw")), run_name="__main__")
