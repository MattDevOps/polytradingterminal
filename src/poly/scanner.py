"""Headless scanner – runs one engine cycle and sends a Windows toast
notification when any market hits ENTER or STRONG ENTER."""

from __future__ import annotations

import asyncio
import logging

from .engine import Engine
from .models import Signal

log = logging.getLogger(__name__)

ALERT_SIGNALS = {Signal.ENTER, Signal.STRONG_ENTER}


async def _scan() -> None:
    engine = Engine()
    try:
        log.info("Scanner: starting engine cycle")
        state = await engine.refresh()

        if state.error:
            log.error("Engine error: %s", state.error)
            return

        hits = [ms for ms in state.markets if ms.signal in ALERT_SIGNALS]

        if not hits:
            print(f"Scan complete: {len(state.markets)} markets scanned — no ENTER/STRONG ENTER signals.")
            return

        # Build notification body
        lines: list[str] = []
        for ms in hits:
            q = ms.market.question
            if len(q) > 60:
                q = q[:58] + "..."
            lines.append(f"[{ms.signal.value}] {q}  (score {ms.composite:.2f})")

        title = f"Poly Scanner: {len(hits)} signal{'s' if len(hits) != 1 else ''} found"
        body = "\n".join(lines[:5])  # cap at 5 to fit in toast
        if len(hits) > 5:
            body += f"\n...and {len(hits) - 5} more"

        _send_notification(title, body)
        log.info("Scanner: notified for %d market(s)", len(hits))

    finally:
        await engine.close()


def _send_notification(title: str, body: str) -> None:
    """Send a Windows toast notification via winotify."""
    try:
        from winotify import Notification

        toast = Notification(
            app_id="Poly Trading Terminal",
            title=title,
            msg=body,
            duration="long",
        )
        toast.show()
    except ImportError:
        # Fallback: use PowerShell BurntToast or basic balloon tip
        log.warning("winotify not installed, falling back to console output")
        print(f"\n{'=' * 60}")
        print(f"  {title}")
        print(f"{'=' * 60}")
        print(body)
        print(f"{'=' * 60}\n")
    except Exception as exc:
        log.error("Failed to send notification: %s", exc)
        print(f"{title}\n{body}")


def run_scan() -> None:
    """Entry point for the scanner."""
    asyncio.run(_scan())
