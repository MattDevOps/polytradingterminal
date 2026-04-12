"""Entry point for the Poly Trading Terminal."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import warnings

# Suppress numpy divide-by-zero warnings (we handle NaN ourselves)
warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")


def _fix_windows_encoding() -> None:
    """Ensure stdout can handle UTF-8 on Windows."""
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def run() -> None:
    _fix_windows_encoding()

    parser = argparse.ArgumentParser(description="Poly Trading Terminal")
    parser.add_argument(
        "--headless", action="store_true",
        help="Print scored markets to stdout instead of launching the TUI",
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="Launch the windowed GUI instead of the terminal TUI",
    )
    parser.add_argument(
        "--top", type=int, default=20,
        help="Number of markets to show in headless mode",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.headless:
        asyncio.run(_headless(args.top))
    elif args.gui:
        _launch_gui()
    else:
        _launch_tui()


def _launch_tui() -> None:
    from .dashboard import PolyTerminal
    app = PolyTerminal()
    app.run()


def _launch_gui() -> None:
    from .gui import launch_gui
    launch_gui()


async def _headless(top_n: int) -> None:
    from .engine import Engine

    engine = Engine()
    try:
        print("Fetching markets and computing scores...\n")
        state = await engine.refresh()

        if state.error:
            print(f"Error: {state.error}")

        # Header
        print(f"{'SCORE':>6}  {'SIGNAL':<14} {'PRICE':>6}  {'DIV':>5} {'DISP':>5} {'VEL':>5} {'PAIR':>5}  MARKET")
        print("─" * 100)

        for ms in state.markets[:top_n]:
            q = ms.market.question
            if len(q) > 48:
                q = q[:46] + "…"
            price = ms.market.outcome_prices[0] if ms.market.outcome_prices else 0.0
            print(
                f"{ms.composite:6.3f}  {ms.signal.value:<14} {price:6.2f}"
                f"  {ms.divergence.value:5.2f} {ms.disposition.value:5.2f}"
                f"  {ms.velocity.value:5.2f} {ms.pairs.value:5.2f}  {q}"
            )

        if state.pair_signals:
            print(f"\n{'─' * 100}")
            print(f"PAIR DIVERGENCES ({len(state.pair_signals)}):\n")
            for ps in state.pair_signals[:10]:
                print(f"  r={ps.correlation:+.2f}  z={ps.z_score:.1f}  {ps.direction}")

        if state.alerts:
            print(f"\n{'─' * 100}")
            print("ALERTS:\n")
            for a in state.alerts:
                print(f"  {a}")

        print(f"\n✓ {len(state.markets)} markets scored in {state.last_refresh:.1f}s")
    finally:
        await engine.close()


if __name__ == "__main__":
    run()
