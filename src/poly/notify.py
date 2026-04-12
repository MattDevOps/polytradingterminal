"""Desktop toast notifications for held-position alerts."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Path to the GUI launcher script (project root)
_GUI_LAUNCHER = Path(__file__).resolve().parents[2] / "poly_gui.pyw"


def _patch_winotify() -> None:
    """Monkey-patch winotify._run_ps to use CREATE_NO_WINDOW so the
    PowerShell process never flashes a visible console window."""
    if sys.platform != "win32":
        return
    import winotify as _wn

    _orig_run_ps = _wn._run_ps

    def _quiet_run_ps(*, file="", command=""):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE

        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass",
        ]
        if file and command:
            raise ValueError
        elif file:
            cmd.extend(["-file", file])
        elif command:
            cmd.extend(["-Command", command])
        else:
            raise ValueError

        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    _wn._run_ps = _quiet_run_ps


def _gui_is_open() -> bool:
    """Return True if the GUI window is currently running."""
    if sys.platform != "win32":
        return False
    try:
        from ctypes import windll
        # Match the Tk window class so we only detect the real GUI,
        # not notification windows or other titled windows.
        hwnd = windll.user32.FindWindowW("TkTopLevel", "POLY TRADING TERMINAL")
        return bool(hwnd)
    except Exception:
        return False


def send_toast(title: str, body: str, url: str | None = None) -> None:
    """Send a Windows toast notification. Suppressed when the GUI is open."""
    if _gui_is_open():
        log.debug("GUI is open – suppressing toast")
        return
    try:
        from winotify import Notification

        _patch_winotify()
        gui_path = str(_GUI_LAUNCHER) if _GUI_LAUNCHER.exists() else ""
        toast = Notification(
            app_id="Poly Trading Terminal",
            title=title,
            msg=body,
            duration="long",
            launch=gui_path,
        )
        if url:
            toast.add_actions(label="View on Polymarket", launch=url)
        toast.show()
    except ImportError:
        log.warning("winotify not installed — printing to console")
        print(f"\n  [{title}] {body}\n")
    except Exception as exc:
        log.error("Toast failed: %s", exc)
