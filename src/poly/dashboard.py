"""Textual TUI dashboard for the Poly Trading Terminal."""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from .engine import Engine, EngineState
from .models import MarketScore, Signal

REFRESH_SECONDS = 45

# ── Styling helpers ───────────────────────────────────────────────────────

_SIG_STYLE = {
    Signal.STRONG_ENTER: "bold white on dark_green",
    Signal.ENTER:        "bold green",
    Signal.HOLD:         "yellow",
    Signal.EXIT:         "bold red",
    Signal.NEUTRAL:      "dim",
}


def _sig_text(sig: Signal) -> Text:
    return Text(f" {sig.value} ", style=_SIG_STYLE.get(sig, ""))


def _bar(val: float, width: int = 4) -> Text:
    filled = round(val * width)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    color = "green" if val >= 0.6 else ("yellow" if val >= 0.35 else "red")
    return Text(bar, style=color)


# ── Detail panel widget ──────────────────────────────────────────────────

class DetailPanel(Static):

    def set_market(self, ms: MarketScore | None) -> None:
        if ms is None:
            self.update(Text("  Select a market", style="dim italic"))
            return

        m = ms.market
        t = Text()
        t.append(m.question + "\n\n", style="bold")

        price = m.outcome_prices[0] if m.outcome_prices else 0
        t.append(f"  Price  {price:.2f}", style="bold")
        t.append(f"    Vol24h ${m.volume_24h:,.0f}", style="dim")
        t.append(f"    Liq ${m.liquidity:,.0f}\n\n", style="dim")

        for f in ms.factors:
            name = f.name.upper()
            n = round(f.value * 10)
            bar_str = "\u2588" * n + "\u2591" * (10 - n)
            color = "green" if f.value >= 0.6 else ("yellow" if f.value >= 0.35 else "red")
            t.append(f"  {name:<13} ", style="bold")
            t.append(bar_str + " ", style=color)
            t.append(f"{f.value:.2f}\n", style="bold " + color)
            if f.details:
                t.append(f"    {f.details}\n", style="dim")

        t.append(f"\n  {'COMPOSITE':<13} ", style="bold")
        t.append(f"{ms.composite:.3f}\n", style="bold")

        style = _SIG_STYLE.get(ms.signal, "")
        t.append(f"  {'SIGNAL':<13} ", style="bold")
        t.append(f" {ms.signal.value} \n", style=style)

        self.update(t)


# ── Main application ─────────────────────────────────────────────────────

class PolyTerminal(App):

    CSS = """
    #main-area {
        height: 1fr;
    }
    #scanner-box {
        width: 3fr;
        border: solid $accent;
    }
    #detail-box {
        width: 2fr;
        border: solid $accent;
        overflow-y: auto;
    }
    #alerts-box {
        height: auto;
        max-height: 14;
        border: solid $warning;
    }
    .panel-title {
        dock: top;
        width: 100%;
        padding: 0 1;
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #alerts-title {
        background: $warning;
    }
    #details {
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
    }
    RichLog {
        height: auto;
        max-height: 10;
    }
    """

    TITLE = "POLY TRADING TERMINAL"
    SUB_TITLE = "Four-Factor Prediction Market Scanner"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("o", "open_market", "Open in Browser"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.engine = Engine()
        self._scored: list[MarketScore] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            with Vertical(id="scanner-box"):
                yield Static("MARKET SCANNER", classes="panel-title")
                yield DataTable(id="scanner", cursor_type="row")
            with Vertical(id="detail-box"):
                yield Static("FACTOR ANALYSIS", classes="panel-title")
                yield DetailPanel(id="details")
        with Vertical(id="alerts-box"):
            yield Static("SIGNALS", classes="panel-title", id="alerts-title")
            yield RichLog(id="alerts", max_lines=200)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#scanner", DataTable)
        table.add_columns("SC", "MARKET", "PRICE", "SIGNAL")
        await self._do_refresh()
        self.set_interval(REFRESH_SECONDS, self._do_refresh)

    async def _do_refresh(self) -> None:
        alerts = self.query_one("#alerts", RichLog)
        alerts.write(Text(" Scanning...", style="dim italic"))

        try:
            state = await self.engine.refresh()
        except Exception as exc:
            alerts.write(Text(f" Error: {exc}", style="bold red"))
            return

        if state.error:
            alerts.write(Text(f" {state.error}", style="bold red"))

        self._scored = state.markets
        self._rebuild_table()

        for a in state.alerts:
            color = "bold green" if "\u25b2" in a else ("bold red" if "\u25bc" in a else "cyan")
            alerts.write(Text(a, style=color))

        n = len(state.markets)
        t = state.last_refresh
        alerts.write(Text(f" Cycle {state.cycle}: {n} markets in {t:.1f}s", style="dim"))

        self._show_detail()

    def _rebuild_table(self) -> None:
        table = self.query_one("#scanner", DataTable)
        table.clear()

        for ms in self._scored:
            q = ms.market.question
            if len(q) > 36:
                q = q[:34] + ".."

            price = ms.market.outcome_prices[0] if ms.market.outcome_prices else 0.0

            table.add_row(
                _bar(ms.composite, 3),
                q,
                Text(f"{price:.2f}", style="bold"),
                _sig_text(ms.signal),
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail()

    def _show_detail(self) -> None:
        detail = self.query_one("#details", DetailPanel)
        table = self.query_one("#scanner", DataTable)
        idx = table.cursor_row
        if idx is not None and 0 <= idx < len(self._scored):
            detail.set_market(self._scored[idx])
        else:
            detail.set_market(None)

    def action_open_market(self) -> None:
        """Open the selected market on polymarket.com."""
        table = self.query_one("#scanner", DataTable)
        idx = table.cursor_row
        if idx is not None and 0 <= idx < len(self._scored):
            ms = self._scored[idx]
            slug = ms.market.event_slug or ms.market.slug
            if slug:
                webbrowser.open(f"https://polymarket.com/event/{slug}")

    async def action_force_refresh(self) -> None:
        await self._do_refresh()

    async def action_quit(self) -> None:
        await self.engine.close()
        self.exit()
