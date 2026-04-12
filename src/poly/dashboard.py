"""Textual TUI dashboard for the Poly Trading Terminal."""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

from .engine import Engine, EngineState
from .models import MarketScore, Signal
from .portfolio import Position

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

    def set_market(self, ms: MarketScore | None, position: Position | None = None) -> None:
        if ms is None:
            self.update(Text("  Select a market", style="dim italic"))
            return

        m = ms.market
        t = Text()
        t.append(m.question + "\n\n", style="bold")

        # Pick recommendation
        pick_style = "bold white on dark_green" if ms.pick_is_yes else "bold white on red"
        t.append("  PICK  ", style="bold")
        t.append(f"  BUY {ms.pick_label}  ", style=pick_style)
        conf_pct = ms.pick_confidence * 100
        t.append(f"  ({conf_pct:.0f}% confidence)\n\n", style="dim")

        price = m.outcome_prices[0] if m.outcome_prices else 0
        t.append(f"  Price  {price:.2f}", style="bold")
        t.append(f"    Vol24h ${m.volume_24h:,.0f}", style="dim")
        t.append(f"    Liq ${m.liquidity:,.0f}\n\n", style="dim")

        # Position P&L (if held)
        if position is not None:
            pnl_color = "bold green" if position.pnl_pct >= 0 else "bold red"
            if position.resolved:
                status_label = "WIN" if position.status == "won" else "LOSS"
                status_style = "bold white on dark_green" if position.status == "won" else "bold white on red"
                t.append(f"  ── YOUR POSITION ── ", style="bold cyan")
                t.append(f" {status_label} ", style=status_style)
                t.append("\n")
            else:
                t.append("  ── YOUR POSITION ──\n", style="bold cyan")
            t.append(f"  Side   {position.side}\n", style="bold")
            t.append(f"  Entry  {position.entry_price:.2f}", style="bold")
            t.append(f"  →  Now  {position.current_price:.2f}\n", style="bold")
            t.append(f"  P&L    ", style="bold")
            t.append(f"{position.pnl_pct:+.1%}", style=pnl_color)
            if position.shares != 1.0:
                t.append(f"  (${position.pnl_abs:+.2f} on {position.shares:.0f} shares)", style=pnl_color)
            t.append("\n\n")

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


# ── Edit position modal ──────────────────────────────────────────────────

class EditPositionScreen(ModalScreen[tuple[float, float] | None]):
    """A modal dialog to edit shares and entry price of an existing position."""

    CSS = """
    EditPositionScreen {
        align: center middle;
    }
    #edit-dialog {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #edit-dialog Label {
        width: 100%;
        margin-bottom: 1;
    }
    #edit-dialog Input {
        margin-bottom: 1;
    }
    #edit-buttons {
        width: 100%;
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #edit-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, question: str, shares: float, entry_price: float) -> None:
        super().__init__()
        self._question = question
        self._shares = shares
        self._entry_price = entry_price

    def compose(self) -> ComposeResult:
        q = self._question[:48] + ".." if len(self._question) > 50 else self._question
        with Vertical(id="edit-dialog"):
            yield Label(f"Edit Position\n{q}")
            yield Label("Entry price:")
            yield Input(value=f"{self._entry_price:.2f}", id="edit-price")
            yield Label("Shares:")
            yield Input(value=f"{self._shares:g}", id="edit-shares")
            with Horizontal(id="edit-buttons"):
                yield Button("Save", variant="primary", id="edit-save")
                yield Button("Cancel", id="edit-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-save":
            try:
                price = float(self.query_one("#edit-price", Input).value)
                shares = float(self.query_one("#edit-shares", Input).value)
            except ValueError:
                return
            self.dismiss((price, shares))
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


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
        Binding("b", "add_position", "Buy/Track"),
        Binding("e", "edit_position", "Edit Position"),
        Binding("s", "remove_position", "Sell/Untrack"),
        Binding("z", "undo", "Undo"),
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
        table.add_columns("SC", "MARKET", "PICK", "PRICE", "SIGNAL")
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
        portfolio = self.engine.portfolio

        # Show resolved positions (not in active scan) at the top
        scored_ids = {ms.market.id for ms in self._scored}
        for pos in portfolio.positions:
            if pos.market_id in scored_ids or not pos.resolved:
                continue
            q = "* " + pos.question
            if len(q) > 36:
                q = q[:34] + ".."
            result_label = "WIN" if pos.status == "won" else "LOSS"
            result_style = "bold white on dark_green" if pos.status == "won" else "bold white on red"
            q_style = "bold green on #1a2f1a" if pos.status == "won" else "bold red on #2f1a1a"
            table.add_row(
                Text("---", style="dim"),
                Text(q, style=q_style),
                Text(f" {pos.side} ", style="dim"),
                Text(f" {result_label} {pos.pnl_pct:+.0%} ", style=result_style),
                Text(" RESOLVED ", style=result_style),
            )

        for ms in self._scored:
            q = ms.market.question
            held = portfolio.has(ms.market.id)
            if held:
                q = "* " + q
            if len(q) > 36:
                q = q[:34] + ".."

            price = ms.market.outcome_prices[0] if ms.market.outcome_prices else 0.0
            pick_style = "bold white on dark_green" if ms.pick_is_yes else "bold white on red"
            label = ms.pick_label
            if len(label) > 14:
                label = label[:12] + ".."
            pick_text = Text(f" {label} ", style=pick_style)

            # Show P&L instead of raw price if position is held
            if held:
                pos = portfolio.get(ms.market.id)
                if pos and pos.resolved:
                    label = "WIN" if pos.status == "won" else "LOSS"
                    style = "bold white on dark_green" if pos.status == "won" else "bold white on red"
                    price_text = Text(f" {label} {pos.pnl_pct:+.0%} ", style=style)
                else:
                    pnl_style = "bold green" if pos and pos.pnl_pct >= 0 else "bold red"
                    price_text = Text(f"{price:.2f} {pos.pnl_pct:+.0%}" if pos else f"{price:.2f}", style=pnl_style)
            else:
                price_text = Text(f"{price:.2f}", style="bold")

            # Style market name with obvious highlight if position is held
            if held:
                q_text = Text(q, style="bold green on #1a2f1a")
            else:
                q_text = q

            table.add_row(
                _bar(ms.composite, 3),
                q_text,
                pick_text,
                price_text,
                _sig_text(ms.signal),
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail()

    def _show_detail(self) -> None:
        detail = self.query_one("#details", DetailPanel)
        table = self.query_one("#scanner", DataTable)
        idx = table.cursor_row
        if idx is not None and 0 <= idx < len(self._scored):
            ms = self._scored[idx]
            pos = self.engine.portfolio.get(ms.market.id)
            detail.set_market(ms, position=pos)
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

    def action_add_position(self) -> None:
        """Track the selected market as a bought position at current price."""
        table = self.query_one("#scanner", DataTable)
        alerts = self.query_one("#alerts", RichLog)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._scored):
            return

        ms = self._scored[idx]
        portfolio = self.engine.portfolio

        if portfolio.has(ms.market.id):
            alerts.write(Text(f" Already tracking: {ms.market.question[:50]}", style="yellow"))
            return

        # Use the pick direction to determine side, and current price as entry
        side = ms.pick_label
        price_idx = 0 if ms.pick_is_yes else (1 if len(ms.market.outcome_prices) > 1 else 0)
        entry_price = ms.market.outcome_prices[price_idx] if ms.market.outcome_prices else 0.0

        pos = Position(
            market_id=ms.market.id,
            question=ms.market.question,
            side=side,
            entry_price=entry_price,
        )
        portfolio.add(pos)

        alerts.write(Text(
            f" + TRACKED: {side} @ {entry_price:.2f} — {ms.market.question[:50]}",
            style="bold cyan",
        ))
        self._rebuild_table()
        self._show_detail()

    def action_edit_position(self) -> None:
        """Edit shares or entry price on the selected position."""
        table = self.query_one("#scanner", DataTable)
        alerts = self.query_one("#alerts", RichLog)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._scored):
            return

        ms = self._scored[idx]
        pos = self.engine.portfolio.get(ms.market.id)
        if pos is None:
            alerts.write(Text(f" Not tracking: {ms.market.question[:50]}", style="dim"))
            return

        def on_result(result: tuple[float, float] | None) -> None:
            if result is None:
                return
            entry_price, shares = result
            self.engine.portfolio.update(ms.market.id, entry_price=entry_price, shares=shares)
            alerts.write(Text(
                f" ~ EDITED: {pos.side} x{shares:.0f} @ {entry_price:.2f} — "
                f"{ms.market.question[:40]}",
                style="bold cyan",
            ))
            self._rebuild_table()
            self._show_detail()

        self.push_screen(
            EditPositionScreen(ms.market.question, pos.shares, pos.entry_price),
            on_result,
        )

    def action_remove_position(self) -> None:
        """Stop tracking the selected market position."""
        table = self.query_one("#scanner", DataTable)
        alerts = self.query_one("#alerts", RichLog)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._scored):
            return

        ms = self._scored[idx]
        removed = self.engine.portfolio.remove(ms.market.id)
        if removed:
            pnl = removed.pnl_pct
            style = "bold green" if pnl >= 0 else "bold red"
            alerts.write(Text(
                f" - SOLD: {removed.side} {removed.entry_price:.2f} -> {removed.current_price:.2f} "
                f"({pnl:+.1%}) — {ms.market.question[:40]}",
                style=style,
            ))
            self._rebuild_table()
            self._show_detail()
        else:
            alerts.write(Text(f" Not tracking: {ms.market.question[:50]}", style="dim"))

    def action_undo(self) -> None:
        """Undo the most recent portfolio action."""
        alerts = self.query_one("#alerts", RichLog)
        result = self.engine.portfolio.undo()
        if result:
            alerts.write(Text(f" UNDO: {result}", style="bold yellow"))
            self._rebuild_table()
            self._show_detail()
        else:
            alerts.write(Text(" Nothing to undo", style="dim"))

    async def action_force_refresh(self) -> None:
        await self._do_refresh()

    async def action_quit(self) -> None:
        await self.engine.close()
        self.exit()
