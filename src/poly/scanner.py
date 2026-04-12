"""Headless scanner – runs one engine cycle and sends a Windows toast
notification when any market hits ENTER or STRONG ENTER."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .engine import Engine
from .models import MarketScore, Signal

log = logging.getLogger(__name__)

ALERT_SIGNALS = {Signal.ENTER, Signal.STRONG_ENTER}

# Path to the GUI launcher script (relative to package location)
_GUI_SCRIPT = Path(__file__).resolve().parents[2] / "poly_gui.pyw"


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
            if len(q) > 50:
                q = q[:48] + "..."
            lines.append(f"[{ms.signal.value}] BUY {ms.pick} — {q}  (score {ms.composite:.2f})")

        title = f"Poly Scanner: {len(hits)} signal{'s' if len(hits) != 1 else ''} found"
        body = "\n".join(lines[:5])  # cap at 5 to fit in toast
        if len(hits) > 5:
            body += f"\n...and {len(hits) - 5} more"

        _send_notification(title, body, hits)
        log.info("Scanner: notified for %d market(s)", len(hits))

    finally:
        await engine.close()


def _send_notification(
    title: str, body: str, hits: list[MarketScore] | None = None,
) -> None:
    """Send a Windows toast notification via winotify."""
    try:
        from winotify import Notification

        # Clicking the notification body opens the GUI
        launch_target = str(_GUI_SCRIPT) if _GUI_SCRIPT.exists() else ""

        toast = Notification(
            app_id="Poly Trading Terminal",
            title=title,
            msg=body,
            duration="long",
            launch=launch_target,
        )

        # Add button linking to the first hit on Polymarket
        if hits:
            slug = hits[0].market.event_slug or hits[0].market.slug
            if slug:
                toast.add_actions(
                    label="View on Polymarket",
                    launch=f"https://polymarket.com/event/{slug}",
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
