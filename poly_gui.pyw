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

from poly.gui import launch_gui

launch_gui()
