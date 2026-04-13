"""Background monitor – runs continuous engine cycles and sends Windows
toast notifications for ENTER / STRONG ENTER signals without any visible
window.  Launch via ``pythonw poly_monitor.pyw`` or ``python -m poly --monitor``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from .engine import Engine
from .models import Signal
from .notify import send_toast

log = logging.getLogger(__name__)

ALERT_SIGNALS = {Signal.ENTER, Signal.STRONG_ENTER}

# Refresh intervals (seconds) – mirrors the GUI
REFRESH_NORMAL = 15
REFRESH_FAST = 5

# Restart policy
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 30  # seconds, doubles each attempt

# Log file lives next to poly_monitor.pyw in the project root
_LOG_DIR = Path(__file__).resolve().parents[2]
LOG_FILE = _LOG_DIR / "poly_monitor.log"


def _setup_file_logging() -> None:
    """Configure logging to write to a rotating log file."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s  %(message)s")
    )
    root.addHandler(handler)


async def _loop() -> None:
    engine = Engine()
    notified: set[str] = set()  # market IDs already toasted
    pending_clear: set[str] = set()  # IDs that left ENTER last cycle

    try:
        while True:
            try:
                state = await engine.refresh()
            except Exception:
                log.exception("Monitor: engine cycle failed")
                await asyncio.sleep(REFRESH_NORMAL)
                continue

            if state.error:
                log.error("Monitor: engine error: %s", state.error)
                await asyncio.sleep(REFRESH_NORMAL)
                continue

            # Detect new ENTER / STRONG_ENTER signals
            new_enters = [
                ms for ms in state.markets
                if ms.signal in ALERT_SIGNALS and ms.market.id not in notified
            ]

            if new_enters:
                lines: list[str] = []
                for ms in new_enters[:5]:
                    q = ms.market.question
                    if len(q) > 50:
                        q = q[:48] + "..."
                    lines.append(
                        f"[{ms.signal.value}] BUY {ms.pick_label} — {q}  "
                        f"(score {ms.composite:.2f})"
                    )
                    notified.add(ms.market.id)

                title = (
                    f"Poly Monitor: {len(new_enters)} signal"
                    f"{'s' if len(new_enters) != 1 else ''} found"
                )
                body = "\n".join(lines)
                if len(new_enters) > 5:
                    body += f"\n...and {len(new_enters) - 5} more"
                    for ms in new_enters[5:]:
                        notified.add(ms.market.id)

                slug = new_enters[0].market.event_slug or new_enters[0].market.slug
                url = f"https://polymarket.com/event/{slug}" if slug else None
                send_toast(title, body, url=url)

            # Only re-notify if a market drops out of ENTER for two
            # consecutive cycles, preventing repeated toasts from score jitter.
            current_enter_ids = {
                ms.market.id for ms in state.markets if ms.signal in ALERT_SIGNALS
            }
            gone = notified - current_enter_ids
            notified -= (gone & pending_clear)
            pending_clear = gone

            interval = REFRESH_FAST if state.closing_soon else REFRESH_NORMAL
            log.debug(
                "Monitor: cycle %d done – %d markets, %d signals, next in %ds",
                state.cycle, len(state.markets), len(new_enters) if new_enters else 0,
                interval,
            )
            await asyncio.sleep(interval)

    finally:
        await engine.close()


def run_monitor() -> None:
    """Entry point for the background monitor."""
    # On Windows with pythonw there is no console – redirect stdio to devnull
    # so accidental prints don't crash.
    if sys.platform == "win32":
        import os
        devnull = open(os.devnull, "w")
        if sys.stdout is None or getattr(sys.stdout, "closed", True):
            sys.stdout = devnull
        if sys.stderr is None or getattr(sys.stderr, "closed", True):
            sys.stderr = devnull

    _setup_file_logging()
    log.info("Poly Monitor starting (PID %d)", __import__("os").getpid())

    retries = 0
    while retries < MAX_RETRIES:
        try:
            asyncio.run(_loop())
            break  # clean exit
        except KeyboardInterrupt:
            log.info("Monitor stopped by user")
            break
        except Exception:
            retries += 1
            wait = RETRY_BACKOFF_BASE * (2 ** (retries - 1))
            log.exception(
                "Monitor crashed (attempt %d/%d) – restarting in %ds",
                retries, MAX_RETRIES, wait,
            )
            time.sleep(wait)

    if retries >= MAX_RETRIES:
        log.critical(
            "Monitor exceeded %d retries – giving up. "
            "Check %s for details.", MAX_RETRIES, LOG_FILE,
        )
        send_toast(
            "Poly Monitor stopped",
            f"Crashed {MAX_RETRIES} times. Check poly_monitor.log for details.",
        )
