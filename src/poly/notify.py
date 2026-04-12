"""Desktop toast notifications for held-position alerts."""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)


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


def send_toast(title: str, body: str, url: str | None = None) -> None:
    """Send a Windows toast notification. Silently falls back to console."""
    try:
        from winotify import Notification

        _patch_winotify()
        toast = Notification(
            app_id="Poly Trading Terminal",
            title=title,
            msg=body,
            duration="long",
        )
        if url:
            toast.add_actions(label="View on Polymarket", launch=url)
        toast.show()
    except ImportError:
        log.warning("winotify not installed — printing to console")
        print(f"\n  [{title}] {body}\n")
    except Exception as exc:
        log.error("Toast failed: %s", exc)
