from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox


def main() -> None:
    try:
        from gui import App
    except RuntimeError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Missing Python package", str(exc))
        sys.exit(1)

    app = App()
    app.mainloop()



if __name__ == "__main__":
    main()

