"""Tkinter GUI for the Poly Trading Terminal.

Runs as a windowed app (.pyw) — no console needed.
Reuses the same Engine and models as the TUI/headless modes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk
from typing import TYPE_CHECKING

from .engine import Engine, EngineState
from .models import MarketScore, Signal
from .portfolio import Portfolio, Position

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

REFRESH_SECONDS = 15
REFRESH_FAST = 5  # used when markets are closing soon

# ── Theme colours ────────────────────────────────────────────────────────

BG         = "#0e1117"
BG_ALT     = "#161b22"
FG         = "#c9d1d9"
FG_DIM     = "#6e7681"
ACCENT     = "#1f6feb"
GREEN      = "#3fb950"
GREEN_BG   = "#0f5323"
YELLOW     = "#d29922"
RED        = "#f85149"
BORDER     = "#30363d"
HEADER_BG  = "#161b22"
SELECT_BG  = "#1c3a5f"
HELD_BG    = "#1a2f1a"          # Dark green tint for active trades
HELD_FG    = "#58d68d"          # Bright green text for active trades

SIG_COLORS = {
    Signal.STRONG_ENTER: GREEN,
    Signal.ENTER:        GREEN,
    Signal.HOLD:         YELLOW,
    Signal.EXIT:         RED,
    Signal.NEUTRAL:      FG_DIM,
}

BAR_CHARS = {
    "green":  "\u2588",
    "yellow": "\u2588",
    "red":    "\u2588",
    "empty":  "\u2591",
}


def _bar_text(val: float, width: int = 10) -> str:
    filled = round(val * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _bar_color(val: float) -> str:
    if val >= 0.60:
        return GREEN
    if val >= 0.35:
        return YELLOW
    return RED


# ── Async bridge ─────────────────────────────────────────────────────────

class _AsyncBridge:
    """Runs an asyncio event loop in a daemon thread so tkinter stays responsive."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro, callback=None):
        """Schedule *coro* on the background loop; call *callback(result)* on completion."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if callback:
            fut.add_done_callback(lambda f: callback(f.result()))

    def shutdown(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)


# ── Main application window ─────────────────────────────────────────────

class PolyGUI:

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("POLY TRADING TERMINAL")
        self.root.geometry("1280x780")
        self.root.minsize(900, 500)
        self.root.configure(bg=BG)

        # Try to set dark title bar on Windows 11
        try:
            from ctypes import windll, byref, sizeof, c_int
            hwnd = windll.user32.GetParent(self.root.winfo_id())
            value = c_int(2)
            windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(value), sizeof(value))
        except Exception:
            pass

        self._engine = Engine()
        self._bridge = _AsyncBridge()
        self._scored: list[MarketScore] = []
        self._state: EngineState | None = None
        self._refresh_job: str | None = None
        self._notification_log: list[tuple[str, str, str]] = []  # (timestamp, text, tag)

        self._build_styles()
        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._initial_refresh)

    # ── Styles ───────────────────────────────────────────────────────

    def _build_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        # Global
        style.configure(".", background=BG, foreground=FG, fieldbackground=BG,
                         bordercolor=BORDER, darkcolor=BG, lightcolor=BG,
                         troughcolor=BG_ALT, arrowcolor=FG)

        # Treeview (market scanner)
        style.configure("Scanner.Treeview",
                         background=BG, foreground=FG, fieldbackground=BG,
                         rowheight=28, font=("Consolas", 10))
        style.configure("Scanner.Treeview.Heading",
                         background=HEADER_BG, foreground=ACCENT,
                         font=("Consolas", 10, "bold"), borderwidth=0)
        style.map("Scanner.Treeview",
                   background=[("selected", SELECT_BG)],
                   foreground=[("selected", FG)])

        # Buttons
        style.configure("Action.TButton",
                         background=ACCENT, foreground="#ffffff",
                         font=("Segoe UI", 9, "bold"), padding=(12, 4))
        style.map("Action.TButton",
                   background=[("active", "#388bfd")])

        # Labels
        style.configure("Title.TLabel",
                         background=HEADER_BG, foreground=ACCENT,
                         font=("Consolas", 11, "bold"), padding=(8, 4))
        style.configure("Status.TLabel",
                         background=BG, foreground=FG_DIM,
                         font=("Consolas", 9))

        # Frame
        style.configure("Card.TFrame", background=BG_ALT, borderwidth=1, relief="solid")

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Top bar
        top = tk.Frame(self.root, bg=HEADER_BG, height=40)
        top.pack(fill="x")
        top.pack_propagate(False)

        tk.Label(top, text="POLY TRADING TERMINAL",
                 bg=HEADER_BG, fg=ACCENT,
                 font=("Consolas", 14, "bold")).pack(side="left", padx=12)
        tk.Label(top, text="Four-Factor Prediction Market Scanner",
                 bg=HEADER_BG, fg=FG_DIM,
                 font=("Consolas", 9)).pack(side="left", padx=(0, 20))

        self._status_var = tk.StringVar(value="Starting...")
        tk.Label(top, textvariable=self._status_var,
                 bg=HEADER_BG, fg=FG_DIM,
                 font=("Consolas", 9)).pack(side="right", padx=12)

        btn_frame = tk.Frame(top, bg=HEADER_BG)
        btn_frame.pack(side="right", padx=8)
        self._refresh_btn = ttk.Button(btn_frame, text="Refresh",
                                        style="Action.TButton",
                                        command=self._manual_refresh)
        self._refresh_btn.pack()

        # Main paned area
        paned = tk.PanedWindow(self.root, orient="horizontal",
                                bg=BORDER, sashwidth=3, sashrelief="flat",
                                opaqueresize=True)
        paned.pack(fill="both", expand=True, padx=4, pady=(2, 0))

        # Left: scanner
        left = tk.Frame(paned, bg=BG)
        paned.add(left, stretch="always", width=700)
        self._build_scanner(left)

        # Right: detail
        right = tk.Frame(paned, bg=BG)
        paned.add(right, stretch="always", width=450)
        self._build_detail(right)

        # Bottom: alerts + notification log
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=4, pady=4)

        alerts_frame = tk.Frame(bottom, bg=BG_ALT, bd=1, relief="solid",
                                 highlightbackground=BORDER, highlightthickness=1)
        alerts_frame.pack(fill="x")
        self._build_alerts(alerts_frame)

        log_frame = tk.Frame(bottom, bg=BG_ALT, bd=1, relief="solid",
                              highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="x", pady=(2, 0))
        self._build_notification_log(log_frame)

    def _build_scanner(self, parent: tk.Frame) -> None:
        hdr = tk.Label(parent, text="  MARKET SCANNER", anchor="w",
                       bg=ACCENT, fg="#ffffff", font=("Consolas", 10, "bold"))
        hdr.pack(fill="x")

        cols = ("composite", "market", "pick", "price", "div", "disp", "vel", "pair", "signal")
        self._tree = ttk.Treeview(parent, columns=cols, show="headings",
                                   style="Scanner.Treeview", selectmode="browse")

        self._tree.heading("composite", text="SCORE")
        self._tree.heading("market",    text="MARKET")
        self._tree.heading("pick",      text="PICK")
        self._tree.heading("price",     text="PRICE")
        self._tree.heading("div",       text="DIV")
        self._tree.heading("disp",      text="DISP")
        self._tree.heading("vel",       text="VEL")
        self._tree.heading("pair",      text="PAIR")
        self._tree.heading("signal",    text="SIGNAL")

        self._tree.column("composite", width=65,  anchor="center", stretch=False)
        self._tree.column("market",    width=240, anchor="w")
        self._tree.column("pick",      width=140, anchor="center", stretch=False)
        self._tree.column("price",     width=60,  anchor="center", stretch=False)
        self._tree.column("div",       width=50,  anchor="center", stretch=False)
        self._tree.column("disp",      width=50,  anchor="center", stretch=False)
        self._tree.column("vel",       width=50,  anchor="center", stretch=False)
        self._tree.column("pair",      width=50,  anchor="center", stretch=False)
        self._tree.column("signal",    width=100, anchor="center", stretch=False)

        scroll = ttk.Scrollbar(parent, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)

        self._tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Double-1>", self._on_double_click)

        # Tag colors for signal rows
        self._tree.tag_configure("strong_enter", foreground=GREEN)
        self._tree.tag_configure("enter",        foreground=GREEN)
        self._tree.tag_configure("hold",         foreground=YELLOW)
        self._tree.tag_configure("exit",         foreground=RED)
        self._tree.tag_configure("neutral",      foreground=FG_DIM)

        # Held position rows — bright green text on dark green background
        self._tree.tag_configure("held", background=HELD_BG, foreground=HELD_FG)

    def _build_detail(self, parent: tk.Frame) -> None:
        hdr = tk.Label(parent, text="  FACTOR ANALYSIS", anchor="w",
                       bg=ACCENT, fg="#ffffff", font=("Consolas", 10, "bold"))
        hdr.pack(fill="x")

        self._detail = tk.Text(parent, bg=BG_ALT, fg=FG, wrap="word",
                                font=("Consolas", 10), bd=0,
                                highlightthickness=0, padx=10, pady=10,
                                cursor="arrow", state="disabled")
        self._detail.pack(fill="both", expand=True)

        # Text tags for styling
        self._detail.tag_configure("title",   font=("Consolas", 12, "bold"), foreground=FG)
        self._detail.tag_configure("label",   font=("Consolas", 10, "bold"), foreground=FG)
        self._detail.tag_configure("dim",     foreground=FG_DIM)
        self._detail.tag_configure("value",   font=("Consolas", 10, "bold"))
        self._detail.tag_configure("green",   foreground=GREEN)
        self._detail.tag_configure("yellow",  foreground=YELLOW)
        self._detail.tag_configure("red",     foreground=RED)
        self._detail.tag_configure("sig_strong_enter", foreground=GREEN, font=("Consolas", 12, "bold"))
        self._detail.tag_configure("sig_enter",   foreground=GREEN, font=("Consolas", 12, "bold"))
        self._detail.tag_configure("sig_hold",    foreground=YELLOW, font=("Consolas", 12, "bold"))
        self._detail.tag_configure("sig_exit",    foreground=RED, font=("Consolas", 12, "bold"))
        self._detail.tag_configure("sig_neutral", foreground=FG_DIM, font=("Consolas", 12, "bold"))
        self._detail.tag_configure("bar_green",  foreground=GREEN, font=("Consolas", 11))
        self._detail.tag_configure("bar_yellow", foreground=YELLOW, font=("Consolas", 11))
        self._detail.tag_configure("bar_red",    foreground=RED, font=("Consolas", 11))
        self._detail.tag_configure("composite_label", font=("Consolas", 11, "bold"),
                                    foreground=ACCENT)
        self._detail.tag_configure("composite_val", font=("Consolas", 14, "bold"),
                                    foreground=FG)
        self._detail.tag_configure("pick_yes", font=("Consolas", 16, "bold"),
                                    foreground="#000000", background=GREEN)
        self._detail.tag_configure("pick_no", font=("Consolas", 16, "bold"),
                                    foreground="#000000", background=RED)
        self._detail.tag_configure("pick_label", font=("Consolas", 11, "bold"),
                                    foreground=ACCENT)
        self._detail.tag_configure("link", foreground=ACCENT, underline=True,
                                    font=("Consolas", 10))
        self._detail.tag_configure("pos_header", font=("Consolas", 11, "bold"),
                                    foreground="#58a6ff")
        self._detail.tag_configure("pos_green", font=("Consolas", 11, "bold"),
                                    foreground=GREEN)
        self._detail.tag_configure("pos_red", font=("Consolas", 11, "bold"),
                                    foreground=RED)

        # Buttons below detail panel
        btn_row = tk.Frame(parent, bg=BG)
        btn_row.pack(fill="x", padx=8, pady=(2, 6))

        self._poly_btn = ttk.Button(btn_row, text="View on Polymarket",
                                     style="Action.TButton",
                                     command=self._open_polymarket)
        self._poly_btn.pack(side="left", expand=True, fill="x", padx=(0, 2))

        self._buy_btn = ttk.Button(btn_row, text="Buy / Track",
                                    style="Action.TButton",
                                    command=self._add_position)
        self._buy_btn.pack(side="left", expand=True, fill="x", padx=2)

        self._edit_btn = ttk.Button(btn_row, text="Edit Position",
                                     style="Action.TButton",
                                     command=self._edit_position)
        self._edit_btn.pack(side="left", expand=True, fill="x", padx=2)

        self._sell_btn = ttk.Button(btn_row, text="Sell / Untrack",
                                     style="Action.TButton",
                                     command=self._remove_position)
        self._sell_btn.pack(side="left", expand=True, fill="x", padx=2)

        self._undo_btn = ttk.Button(btn_row, text="Undo",
                                     style="Action.TButton",
                                     command=self._undo_action)
        self._undo_btn.pack(side="left", expand=True, fill="x", padx=(2, 0))

    def _build_alerts(self, parent: tk.Frame) -> None:
        hdr = tk.Label(parent, text="  SIGNALS & ALERTS", anchor="w",
                       bg=YELLOW, fg=BG, font=("Consolas", 10, "bold"))
        hdr.pack(fill="x")

        self._alerts = tk.Text(parent, bg=BG_ALT, fg=FG, wrap="word",
                                font=("Consolas", 9), bd=0,
                                highlightthickness=0, height=6,
                                padx=8, pady=4, cursor="arrow", state="disabled")
        self._alerts.pack(fill="x")

        self._alerts.tag_configure("up",     foreground=GREEN)
        self._alerts.tag_configure("down",   foreground=RED)
        self._alerts.tag_configure("pair",   foreground="#58a6ff")
        self._alerts.tag_configure("dim",    foreground=FG_DIM)
        self._alerts.tag_configure("profit", foreground=GREEN, font=("Consolas", 9, "bold"))
        self._alerts.tag_configure("loss",   foreground=RED, font=("Consolas", 9, "bold"))

    def _build_notification_log(self, parent: tk.Frame) -> None:
        hdr = tk.Label(parent, text="  NOTIFICATION LOG", anchor="w",
                       bg=ACCENT, fg="#ffffff", font=("Consolas", 10, "bold"))
        hdr.pack(fill="x")

        self._notif_log = tk.Text(parent, bg=BG_ALT, fg=FG, wrap="word",
                                   font=("Consolas", 9), bd=0,
                                   highlightthickness=0, height=5,
                                   padx=8, pady=4, cursor="arrow", state="disabled")
        self._notif_log.pack(fill="x")

        self._notif_log.tag_configure("profit", foreground=GREEN, font=("Consolas", 9, "bold"))
        self._notif_log.tag_configure("loss",   foreground=RED, font=("Consolas", 9, "bold"))
        self._notif_log.tag_configure("ts",     foreground=FG_DIM)
        self._notif_log.tag_configure("empty",  foreground=FG_DIM)

    # ── Data refresh ─────────────────────────────────────────────────

    def _initial_refresh(self) -> None:
        self._do_refresh()

    def _manual_refresh(self) -> None:
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
        self._do_refresh()

    def _do_refresh(self) -> None:
        self._status_var.set("Scanning...")
        self._refresh_btn.state(["disabled"])
        self._bridge.submit(self._engine.refresh(), callback=self._on_refresh_done)

    def _on_refresh_done(self, state: EngineState) -> None:
        # This runs on the background thread – schedule UI update on main thread
        self.root.after(0, self._apply_state, state)

    def _apply_state(self, state: EngineState) -> None:
        self._state = state
        self._scored = state.markets

        # Rebuild table
        portfolio = self._engine.portfolio
        self._tree.delete(*self._tree.get_children())
        for ms in self._scored:
            q = ms.market.question
            held = portfolio.has(ms.market.id)
            if held:
                q = "* " + q
            if len(q) > 45:
                q = q[:43] + ".."
            price = ms.market.outcome_prices[0] if ms.market.outcome_prices else 0.0
            tag = ms.signal.name.lower()
            label = ms.pick_label
            if len(label) > 14:
                label = label[:12] + ".."
            pick_str = f">> {label} <<"

            # Show P&L next to price if position is held
            if held:
                pos = portfolio.get(ms.market.id)
                price_str = f"{price:.2f} ({pos.pnl_pct:+.0%})" if pos else f"{price:.2f}"
            else:
                price_str = f"{price:.2f}"

            row_tags = (tag, "held") if held else (tag,)
            self._tree.insert("", "end", values=(
                f"{ms.composite:.3f}",
                q,
                pick_str,
                price_str,
                f"{ms.divergence.value:.2f}",
                f"{ms.disposition.value:.2f}",
                f"{ms.velocity.value:.2f}",
                f"{ms.pairs.value:.2f}",
                ms.signal.value,
            ), tags=row_tags)

        # Select first row
        children = self._tree.get_children()
        if children:
            self._tree.selection_set(children[0])
            self._tree.focus(children[0])

        # Capture important alerts to persistent notification log
        now = datetime.now().strftime("%H:%M:%S")
        for a in state.alerts:
            if "$ PROFIT" in a:
                self._notification_log.append((now, a, "profit"))
            elif a.startswith("✓") or a.startswith("✗"):
                tag = "profit" if a.startswith("✓") else "loss"
                self._notification_log.append((now, a, tag))
            elif "! SELL" in a:
                self._notification_log.append((now, a, "loss"))

        # Keep last 50 entries
        self._notification_log = self._notification_log[-50:]

        # Update notification log display
        self._notif_log.configure(state="normal")
        self._notif_log.delete("1.0", "end")
        if not self._notification_log:
            self._notif_log.insert("end", "No notifications yet\n", "empty")
        else:
            for ts, text, tag in self._notification_log:
                self._notif_log.insert("end", f"[{ts}] ", "ts")
                self._notif_log.insert("end", text + "\n", tag)
        self._notif_log.configure(state="disabled")
        self._notif_log.see("end")

        # Update alerts
        self._alerts.configure(state="normal")
        self._alerts.delete("1.0", "end")
        if state.error:
            self._alerts.insert("end", f"Error: {state.error}\n", "down")
        for a in state.alerts:
            if "$ PROFIT" in a:
                tag = "profit"
            elif "! LOSS" in a or "! SELL" in a:
                tag = "loss"
            elif "\u25b2" in a:
                tag = "up"
            elif "\u25bc" in a:
                tag = "down"
            elif "\u21c4" in a or "PAIR" in a:
                tag = "pair"
            else:
                tag = "dim"
            self._alerts.insert("end", a + "\n", tag)

        n = len(state.markets)
        t = state.last_refresh
        self._alerts.insert("end",
                            f"Cycle {state.cycle}: {n} markets scored in {t:.1f}s\n", "dim")
        self._alerts.configure(state="disabled")
        self._alerts.see("end")

        # Adaptive refresh: poll faster when markets are about to close
        interval = REFRESH_FAST if state.closing_soon else REFRESH_SECONDS

        # Status
        fast_tag = " [FAST]" if state.closing_soon else ""
        self._status_var.set(
            f"{n} markets | cycle {state.cycle} | {t:.1f}s | "
            f"next in {interval}s{fast_tag}"
        )
        self._refresh_btn.state(["!disabled"])

        # Schedule next refresh
        self._refresh_job = self.root.after(interval * 1000, self._do_refresh)

    # ── Detail panel ─────────────────────────────────────────────────

    def _on_select(self, _event=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = self._tree.index(sel[0])
        if 0 <= idx < len(self._scored):
            self._show_detail(self._scored[idx])

    def _on_double_click(self, _event=None) -> None:
        """Double-click a market row to open it on Polymarket."""
        self._open_polymarket()

    def _open_polymarket(self) -> None:
        """Open the selected market on polymarket.com in the default browser."""
        sel = self._tree.selection()
        if not sel:
            return
        idx = self._tree.index(sel[0])
        if 0 <= idx < len(self._scored):
            ms = self._scored[idx]
            slug = ms.market.event_slug or ms.market.slug
            if slug:
                webbrowser.open(f"https://polymarket.com/event/{slug}")

    def _show_detail(self, ms: MarketScore) -> None:
        d = self._detail
        d.configure(state="normal")
        d.delete("1.0", "end")

        m = ms.market

        # Title
        d.insert("end", m.question + "\n\n", "title")

        # Pick recommendation — the most important info
        pick_tag = "pick_yes" if ms.pick_is_yes else "pick_no"
        d.insert("end", "  PICK  ", "pick_label")
        d.insert("end", f"  BUY {ms.pick_label}  ", pick_tag)
        conf_pct = ms.pick_confidence * 100
        d.insert("end", f"  ({conf_pct:.0f}% confidence)\n\n", "dim")

        # Price / volume / liquidity
        price = m.outcome_prices[0] if m.outcome_prices else 0.0
        d.insert("end", "Price  ", "label")
        d.insert("end", f"{price:.2f}", "value")
        d.insert("end", f"    Vol24h ${m.volume_24h:,.0f}", "dim")
        d.insert("end", f"    Liq ${m.liquidity:,.0f}\n", "dim")

        # Position P&L (if held)
        pos = self._engine.portfolio.get(m.id)
        if pos is not None:
            pnl_tag = "pos_green" if pos.pnl_pct >= 0 else "pos_red"
            d.insert("end", "\n")
            d.insert("end", "  -- YOUR POSITION --\n", "pos_header")
            d.insert("end", f"  Side   {pos.side}\n", "label")
            d.insert("end", f"  Entry  {pos.entry_price:.2f}", "label")
            d.insert("end", f"  ->  Now  {pos.current_price:.2f}\n", "label")
            d.insert("end", f"  P&L    ", "label")
            d.insert("end", f"{pos.pnl_pct:+.1%}", pnl_tag)
            if pos.shares != 1.0:
                d.insert("end", f"  (${pos.pnl_abs:+.2f} on {pos.shares:.0f} shares)", pnl_tag)
            d.insert("end", "\n")

        if m.spread:
            d.insert("end", f"Spread {m.spread:.4f}", "dim")
            d.insert("end", f"    Bid {m.best_bid:.2f}  Ask {m.best_ask:.2f}\n", "dim")

        d.insert("end", "\n")

        # Factor bars
        for f in ms.factors:
            name = f.name.upper()
            bar = _bar_text(f.value)
            color = "green" if f.value >= 0.60 else ("yellow" if f.value >= 0.35 else "red")
            bar_tag = f"bar_{color}"

            d.insert("end", f"  {name:<13} ", "label")
            d.insert("end", bar + " ", bar_tag)
            d.insert("end", f" {f.value:.2f}\n", bar_tag)
            if f.details:
                d.insert("end", f"    {f.details}\n", "dim")

        # Composite
        d.insert("end", "\n")
        d.insert("end", f"  {'COMPOSITE':<13} ", "composite_label")
        d.insert("end", f"{ms.composite:.3f}\n", "composite_val")

        # Signal
        sig_tag = f"sig_{ms.signal.name.lower()}"
        d.insert("end", f"  {'SIGNAL':<13} ", "composite_label")
        d.insert("end", f" {ms.signal.value} \n", sig_tag)

        # Pair signals for this market (if any)
        if self._state and self._state.pair_signals:
            relevant = [ps for ps in self._state.pair_signals
                        if ps.market_a_id == m.id or ps.market_b_id == m.id]
            if relevant:
                d.insert("end", "\n  PAIR DIVERGENCES\n", "label")
                for ps in relevant:
                    d.insert("end",
                             f"    r={ps.correlation:+.2f}  z={ps.z_score:.1f}  "
                             f"{ps.direction}\n", "dim")

        d.configure(state="disabled")

    # ── Portfolio actions ────────────────────────────────────────────

    def _get_selected_ms(self) -> MarketScore | None:
        sel = self._tree.selection()
        if not sel:
            return None
        idx = self._tree.index(sel[0])
        if 0 <= idx < len(self._scored):
            return self._scored[idx]
        return None

    def _add_position(self) -> None:
        """Open a dialog to record a buy with entry price."""
        ms = self._get_selected_ms()
        if ms is None:
            return

        portfolio = self._engine.portfolio
        if portfolio.has(ms.market.id):
            self._alert_write(f"Already tracking: {ms.market.question[:50]}", "loss")
            return

        # Build the dialog
        dlg = tk.Toplevel(self.root)
        dlg.title("Track Position")
        dlg.geometry("400x280")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        # Center on parent
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 400) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 280) // 2
        dlg.geometry(f"+{x}+{y}")

        tk.Label(dlg, text="Track Position", bg=BG, fg=ACCENT,
                 font=("Consolas", 12, "bold")).pack(pady=(12, 4))

        q = ms.market.question
        if len(q) > 50:
            q = q[:48] + ".."
        tk.Label(dlg, text=q, bg=BG, fg=FG, font=("Consolas", 9),
                 wraplength=380).pack(pady=(0, 8))

        # Side selection
        side_frame = tk.Frame(dlg, bg=BG)
        side_frame.pack(pady=4)
        tk.Label(side_frame, text="Side:", bg=BG, fg=FG,
                 font=("Consolas", 10)).pack(side="left", padx=(0, 8))

        side_var = tk.StringVar(value=ms.pick_label)
        outcomes = ms.market.outcomes or ["Yes", "No"]
        for outcome in outcomes:
            tk.Radiobutton(side_frame, text=outcome, variable=side_var,
                           value=outcome, bg=BG, fg=FG, selectcolor=BG_ALT,
                           activebackground=BG, activeforeground=GREEN,
                           font=("Consolas", 10)).pack(side="left", padx=4)

        # Entry price
        price_frame = tk.Frame(dlg, bg=BG)
        price_frame.pack(pady=4)
        tk.Label(price_frame, text="Entry price:", bg=BG, fg=FG,
                 font=("Consolas", 10)).pack(side="left", padx=(0, 8))

        current_price = ms.market.outcome_prices[0] if ms.market.outcome_prices else 0.0
        price_var = tk.StringVar(value=f"{current_price:.2f}")
        price_entry = tk.Entry(price_frame, textvariable=price_var, width=10,
                               bg=BG_ALT, fg=FG, font=("Consolas", 11),
                               insertbackground=FG, relief="solid", bd=1)
        price_entry.pack(side="left")
        price_entry.select_range(0, "end")
        price_entry.focus_set()

        # Shares
        shares_frame = tk.Frame(dlg, bg=BG)
        shares_frame.pack(pady=4)
        tk.Label(shares_frame, text="Shares:", bg=BG, fg=FG,
                 font=("Consolas", 10)).pack(side="left", padx=(0, 8))

        shares_var = tk.StringVar(value="1")
        shares_entry = tk.Entry(shares_frame, textvariable=shares_var, width=10,
                                bg=BG_ALT, fg=FG, font=("Consolas", 11),
                                insertbackground=FG, relief="solid", bd=1)
        shares_entry.pack(side="left")

        # Confirm / Cancel
        def confirm():
            try:
                entry_price = float(price_var.get())
                shares = float(shares_var.get())
            except ValueError:
                tk.Label(dlg, text="Invalid number", bg=BG, fg=RED,
                         font=("Consolas", 9)).pack()
                return

            pos = Position(
                market_id=ms.market.id,
                question=ms.market.question,
                side=side_var.get(),
                entry_price=entry_price,
                shares=shares,
            )
            portfolio.add(pos)
            self._alert_write(
                f"+ TRACKED: {pos.side} x{shares:.0f} @ {entry_price:.2f} -- "
                f"{ms.market.question[:40]}",
                "profit",
            )
            dlg.destroy()
            # Refresh the table and detail to show the position
            self._apply_state(self._state) if self._state else None

        btn_frame = tk.Frame(dlg, bg=BG)
        btn_frame.pack(pady=12)
        ttk.Button(btn_frame, text="Track", style="Action.TButton",
                   command=confirm).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", style="Action.TButton",
                   command=dlg.destroy).pack(side="left", padx=4)

        dlg.bind("<Return>", lambda _: confirm())
        dlg.bind("<Escape>", lambda _: dlg.destroy())

    def _edit_position(self) -> None:
        """Open a dialog to edit shares and entry price of an existing position."""
        ms = self._get_selected_ms()
        if ms is None:
            return

        portfolio = self._engine.portfolio
        pos = portfolio.get(ms.market.id)
        if pos is None:
            self._alert_write(f"Not tracking: {ms.market.question[:50]}", "dim")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Edit Position")
        dlg.geometry("400x220")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 400) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 220) // 2
        dlg.geometry(f"+{x}+{y}")

        tk.Label(dlg, text="Edit Position", bg=BG, fg=ACCENT,
                 font=("Consolas", 12, "bold")).pack(pady=(12, 4))

        q = ms.market.question
        if len(q) > 50:
            q = q[:48] + ".."
        tk.Label(dlg, text=q, bg=BG, fg=FG, font=("Consolas", 9),
                 wraplength=380).pack(pady=(0, 8))

        # Entry price
        price_frame = tk.Frame(dlg, bg=BG)
        price_frame.pack(pady=4)
        tk.Label(price_frame, text="Entry price:", bg=BG, fg=FG,
                 font=("Consolas", 10)).pack(side="left", padx=(0, 8))
        price_var = tk.StringVar(value=f"{pos.entry_price:.2f}")
        price_entry = tk.Entry(price_frame, textvariable=price_var, width=10,
                               bg=BG_ALT, fg=FG, font=("Consolas", 11),
                               insertbackground=FG, relief="solid", bd=1)
        price_entry.pack(side="left")

        # Shares
        shares_frame = tk.Frame(dlg, bg=BG)
        shares_frame.pack(pady=4)
        tk.Label(shares_frame, text="Shares:     ", bg=BG, fg=FG,
                 font=("Consolas", 10)).pack(side="left", padx=(0, 8))
        shares_var = tk.StringVar(value=f"{pos.shares:g}")
        shares_entry = tk.Entry(shares_frame, textvariable=shares_var, width=10,
                                bg=BG_ALT, fg=FG, font=("Consolas", 11),
                                insertbackground=FG, relief="solid", bd=1)
        shares_entry.pack(side="left")
        shares_entry.select_range(0, "end")
        shares_entry.focus_set()

        def confirm():
            try:
                entry_price = float(price_var.get())
                shares = float(shares_var.get())
            except ValueError:
                tk.Label(dlg, text="Invalid number", bg=BG, fg=RED,
                         font=("Consolas", 9)).pack()
                return

            portfolio.update(ms.market.id, entry_price=entry_price, shares=shares)
            self._alert_write(
                f"~ EDITED: {pos.side} x{shares:.0f} @ {entry_price:.2f} -- "
                f"{ms.market.question[:40]}",
                "profit",
            )
            dlg.destroy()
            if self._state:
                self._apply_state(self._state)

        btn_frame = tk.Frame(dlg, bg=BG)
        btn_frame.pack(pady=12)
        ttk.Button(btn_frame, text="Save", style="Action.TButton",
                   command=confirm).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", style="Action.TButton",
                   command=dlg.destroy).pack(side="left", padx=4)

        dlg.bind("<Return>", lambda _: confirm())
        dlg.bind("<Escape>", lambda _: dlg.destroy())

    def _remove_position(self) -> None:
        """Remove the selected market from the portfolio."""
        ms = self._get_selected_ms()
        if ms is None:
            return

        removed = self._engine.portfolio.remove(ms.market.id)
        if removed:
            pnl = removed.pnl_pct
            tag = "profit" if pnl >= 0 else "loss"
            self._alert_write(
                f"- SOLD: {removed.side} {removed.entry_price:.2f} -> "
                f"{removed.current_price:.2f} ({pnl:+.1%}) -- "
                f"{ms.market.question[:40]}",
                tag,
            )
            if self._state:
                self._apply_state(self._state)
        else:
            self._alert_write(f"Not tracking: {ms.market.question[:50]}", "dim")

    def _undo_action(self) -> None:
        """Undo the most recent portfolio action."""
        result = self._engine.portfolio.undo()
        if result:
            self._alert_write(f"UNDO: {result}", "profit")
            if self._state:
                self._apply_state(self._state)
        else:
            self._alert_write("Nothing to undo", "dim")

    def _alert_write(self, text: str, tag: str) -> None:
        """Write a line to the alerts panel."""
        self._alerts.configure(state="normal")
        self._alerts.insert("end", text + "\n", tag)
        self._alerts.configure(state="disabled")
        self._alerts.see("end")

    # ── Lifecycle ────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
        self._bridge.submit(self._engine.close())
        self._bridge.shutdown()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _focus_existing_window() -> bool:
    """If a GUI instance is already running, bring it to the foreground.

    Returns True if an existing window was found (caller should exit).
    """
    if sys.platform != "win32":
        return False
    try:
        from ctypes import windll
        user32 = windll.user32
        hwnd = user32.FindWindowW(None, "POLY TRADING TERMINAL")
        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def launch_gui() -> None:
    """Entry point for the GUI."""
    import sys
    import warnings

    # Single-instance guard: focus the existing window instead of opening another
    if _focus_existing_window():
        return

    warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")

    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    app = PolyGUI()
    app.run()
