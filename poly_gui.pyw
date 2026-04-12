"""Poly Trading Terminal – Windowed GUI launcher.

Run this file directly (double-click or `pythonw poly_gui.pyw`)
to launch the GUI with no console window.
"""

import sys
from pathlib import Path

# Ensure the src directory is on the import path
src = Path(__file__).resolve().parent / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

try:
    from poly.gui import launch_gui
    launch_gui()
except Exception:
    # pythonw swallows all output — show a messagebox so crashes are visible
    import traceback
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Poly Trading Terminal – Error",
            traceback.format_exc(),
        )
        root.destroy()
    except Exception:
        pass
