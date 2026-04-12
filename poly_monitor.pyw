"""Poly Trading Terminal – Background Monitor launcher.

Run this file directly (double-click or ``pythonw poly_monitor.pyw``)
to start the background signal monitor with NO console window.
Toast notifications appear when ENTER / STRONG ENTER signals are detected.
"""

import sys
from pathlib import Path

# Ensure the src directory is on the import path
src = Path(__file__).resolve().parent / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from poly.monitor import run_monitor

run_monitor()
