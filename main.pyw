"""
DropGain entry point.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox


def main() -> None:
    try:
        from analysis import ensure_bundled_bin_on_path
        from gui_tk import App, enable_crash_diagnostics, enable_windows_dpi_awareness
    except RuntimeError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Missing Python package", str(exc))
        sys.exit(1)

    ensure_bundled_bin_on_path()
    enable_windows_dpi_awareness()
    enable_crash_diagnostics()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()