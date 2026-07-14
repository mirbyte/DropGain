"""Small platform helpers shared by GUI and tooling."""

from __future__ import annotations

import os
import subprocess
import sys


def is_macos() -> bool:
    return sys.platform == "darwin"


def open_in_file_manager(path: str) -> None:
    """Open a file or folder in the platform file manager."""
    resolved = os.path.abspath(path)
    if os.name == "nt":
        os.startfile(resolved)  # type: ignore[attr-defined]
    elif is_macos():
        subprocess.Popen(["open", resolved])
    else:
        subprocess.Popen(["xdg-open", resolved])
