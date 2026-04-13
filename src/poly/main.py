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
        "--scan", action="store_true",
        help="Run one scan cycle and send a toast notification on ENTER/STRONG ENTER signals",
    )
    parser.add_argument(
        "--monitor", action="store_true",
        help="Run a background monitor that sends toast notifications for new signals (no window)",
    )
    parser.add_argument(
        "--portfolio", action="store_true",
        help="Show current portfolio positions and P&L",
    )
    parser.add_argument(
        "--add-position", nargs=3, metavar=("MARKET_ID", "SIDE", "PRICE"),
        help="Add a position: --add-position <market_id> <YES|NO|name> <entry_price>",
    )
    parser.add_argument(
        "--remove-position", metavar="MARKET_ID",
        help="Remove a position by market ID",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Check whether the background monitor is currently running",
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

    if args.status:
        _check_monitor_status()
        return

    if args.portfolio or args.add_position or args.remove_position:
        _portfolio_cli(args)
        return

    if args.monitor:
        from .monitor import run_monitor
        run_monitor()
    elif args.scan:
        from .scanner import run_scan
        run_scan()
    elif args.headless:
        asyncio.run(_headless(args.top))
    elif args.gui:
        _launch_gui()
    else:
        _launch_tui()


def _check_monitor_status() -> None:
    """Check if the background monitor process is running."""
    import subprocess

    ps_script = (
        "Get-CimInstance Win32_Process "
        "-Filter \"name='pythonw.exe' or name='python.exe'\" "
        "| Select-Object ProcessId, CommandLine "
        "| Format-List"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
        )
        monitor_pids: list[str] = []
        current_pid = None
        is_monitor = False
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("ProcessId"):
                current_pid = line.split(":", 1)[1].strip()
            elif line.startswith("CommandLine"):
                cmdline = line.split(":", 1)[1].strip().lower()
                is_monitor = "poly_monitor" in cmdline or "--monitor" in cmdline
            elif not line and current_pid:
                if is_monitor:
                    monitor_pids.append(current_pid)
                current_pid = None
                is_monitor = False
        # Flush last entry
        if current_pid and is_monitor:
            monitor_pids.append(current_pid)

        if monitor_pids:
            print(f"Poly Monitor is RUNNING  ({len(monitor_pids)} process{'es' if len(monitor_pids) > 1 else ''})")
            for pid in monitor_pids:
                print(f"  PID {pid}")
            print("\nTo stop:  taskkill /PID <pid>")
        else:
            print("Poly Monitor is NOT running.")
            print("\nTo start:")
            print("  pythonw poly_monitor.pyw        (background, no window)")
            print("  python -m poly --monitor        (foreground, with console)")
    except Exception as exc:
        print(f"Could not check process status: {exc}")

    # Show recent log tail
    from .monitor import LOG_FILE
    if LOG_FILE.exists():
        print(f"\nLog: {LOG_FILE}")
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-8:]
        if tail:
            print("Last log entries:")
            for ln in tail:
                print(f"  {ln}")


def _portfolio_cli(args) -> None:
    from .portfolio import Portfolio, Position

    portfolio = Portfolio()

    if args.add_position:
        market_id, side, price_str = args.add_position
        pos = Position(
            market_id=market_id,
            question=market_id,  # will show ID if we don't have the name
            side=side,
            entry_price=float(price_str),
        )
        portfolio.add(pos)
        print(f"Added: {side} @ {float(price_str):.2f} for market {market_id}")
        return

    if args.remove_position:
        removed = portfolio.remove(args.remove_position)
        if removed:
            print(f"Removed: {removed.side} @ {removed.entry_price:.2f} — {removed.question}")
        else:
            print(f"No position found for market {args.remove_position}")
        return

    # --portfolio: list all positions
    if not portfolio.positions:
        print("No positions tracked. Use 'b' in the TUI to track a market, or --add-position.")
        return

    print(f"{'SIDE':<16} {'ENTRY':>6} {'NOW':>6} {'P&L':>8}  MARKET")
    print("─" * 80)
    for p in portfolio.positions:
        pnl_str = f"{p.pnl_pct:+.1%}" if p.current_price > 0 else "  n/a"
        now_str = f"{p.current_price:.2f}" if p.current_price > 0 else "  —"
        q = p.question
        if len(q) > 45:
            q = q[:43] + ".."
        print(f"{p.side:<16} {p.entry_price:6.2f} {now_str:>6} {pnl_str:>8}  {q}")

    print(f"\n{len(portfolio.positions)} position(s) tracked")
    print("Tip: Run the TUI to see live P&L updates")


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
        print(f"{'SCORE':>6}  {'SIGNAL':<14} {'PICK':<16} {'PRICE':>6}  {'DIV':>5} {'DISP':>5} {'VEL':>5} {'PAIR':>5}  MARKET")
        print("─" * 115)

        for ms in state.markets[:top_n]:
            q = ms.market.question
            if len(q) > 45:
                q = q[:43] + "…"
            price = ms.market.outcome_prices[0] if ms.market.outcome_prices else 0.0
            label = ms.pick_label
            if len(label) > 16:
                label = label[:14] + ".."
            print(
                f"{ms.composite:6.3f}  {ms.signal.value:<14} {label:<16} {price:6.2f}"
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
