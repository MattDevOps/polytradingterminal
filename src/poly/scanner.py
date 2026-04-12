"""Headless scanner – runs one engine cycle and sends a Windows toast
notification when any market hits ENTER or STRONG ENTER."""

from __future__ import annotations

import asyncio
import logging

from .engine import Engine
from .models import MarketScore, Signal
from .notify import send_toast

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
            # Still report if there are closing-soon alerts in the engine output
            closing = [a for a in state.alerts if "\u23f0" in a]
            if closing:
                print(f"Scan complete: no signals, but {len(closing)} market(s) closing soon.")
            else:
                print(f"Scan complete: {len(state.markets)} markets scanned — no ENTER/STRONG ENTER signals.")
            return

        # Build notification body
        lines: list[str] = []
        for ms in hits:
            q = ms.market.question
            if len(q) > 50:
                q = q[:48] + "..."
            lines.append(f"[{ms.signal.value}] BUY {ms.pick_label} — {q}  (score {ms.composite:.2f})")

        title = f"Poly Scanner: {len(hits)} signal{'s' if len(hits) != 1 else ''} found"
        body = "\n".join(lines[:5])  # cap at 5 to fit in toast
        if len(hits) > 5:
            body += f"\n...and {len(hits) - 5} more"

        url = None
        if hits:
            slug = hits[0].market.event_slug or hits[0].market.slug
            if slug:
                url = f"https://polymarket.com/event/{slug}"
        send_toast(title, body, url=url)
        log.info("Scanner: notified for %d market(s)", len(hits))

    finally:
        await engine.close()


def run_scan() -> None:
    """Entry point for the scanner."""
    asyncio.run(_scan())
