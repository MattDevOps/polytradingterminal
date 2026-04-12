"""Portfolio — tracks open positions and computes P&L against live prices."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / ".poly_positions.json"

# Profit thresholds that trigger alerts (fraction, e.g. 0.25 = +25%)
PROFIT_TARGETS = [0.25, 0.50, 1.0, 2.0]
# Loss threshold that triggers a warning
LOSS_WARN = -0.20


@dataclass
class Position:
    market_id: str
    question: str           # human-readable market name
    side: str               # "YES" or "NO" (or outcome name like "Lucknow")
    entry_price: float      # price paid per share (0.0 – 1.0)
    shares: float = 1.0     # number of shares
    entry_time: float = 0.0 # unix timestamp
    status: str = "open"    # "open", "won", "lost"

    # ── live fields (not persisted, filled at runtime) ──
    current_price: float = 0.0
    pnl_pct: float = 0.0
    pnl_abs: float = 0.0

    def __post_init__(self) -> None:
        if self.entry_time == 0.0:
            self.entry_time = time.time()

    @property
    def resolved(self) -> bool:
        return self.status in ("won", "lost")

    def resolve(self, won: bool) -> None:
        """Mark this position as won or lost after market closure."""
        self.status = "won" if won else "lost"
        self.current_price = 1.0 if won else 0.0
        if self.entry_price > 0:
            self.pnl_pct = (self.current_price - self.entry_price) / self.entry_price
        else:
            self.pnl_pct = 0.0
        self.pnl_abs = (self.current_price - self.entry_price) * self.shares

    def update_pnl(self, current_price: float) -> None:
        """Recompute P&L given the current market price for this side."""
        if self.resolved:
            return  # don't overwrite resolved outcome
        self.current_price = current_price
        if self.entry_price > 0:
            self.pnl_pct = (current_price - self.entry_price) / self.entry_price
        else:
            self.pnl_pct = 0.0
        self.pnl_abs = (current_price - self.entry_price) * self.shares


class Portfolio:
    """Manages a set of open positions with JSON persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self.positions: list[Position] = []
        self._alerted: dict[str, set[float]] = {}  # market_id -> set of targets already alerted
        self.load()

    # ── persistence ──────────────────────────────────────────────────

    def load(self) -> None:
        if not self.path.exists():
            self.positions = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.positions = [
                Position(
                    market_id=d["market_id"],
                    question=d["question"],
                    side=d["side"],
                    entry_price=d["entry_price"],
                    shares=d.get("shares", 1.0),
                    entry_time=d.get("entry_time", 0.0),
                    status=d.get("status", "open"),
                )
                for d in data
            ]
            log.info("Loaded %d positions from %s", len(self.positions), self.path)
        except Exception as exc:
            log.warning("Failed to load portfolio: %s", exc)
            self.positions = []

    def save(self) -> None:
        data = [
            {
                "market_id": p.market_id,
                "question": p.question,
                "side": p.side,
                "entry_price": p.entry_price,
                "shares": p.shares,
                "entry_time": p.entry_time,
                "status": p.status,
            }
            for p in self.positions
        ]
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("Saved %d positions to %s", len(self.positions), self.path)

    # ── operations ───────────────────────────────────────────────────

    def add(self, position: Position) -> None:
        self.positions.append(position)
        self.save()

    def remove(self, market_id: str) -> Position | None:
        for i, p in enumerate(self.positions):
            if p.market_id == market_id:
                removed = self.positions.pop(i)
                self._alerted.pop(market_id, None)
                self.save()
                return removed
        return None

    def get(self, market_id: str) -> Position | None:
        for p in self.positions:
            if p.market_id == market_id:
                return p
        return None

    def update(self, market_id: str, **kwargs) -> Position | None:
        """Update fields on an existing position (e.g. shares, entry_price)."""
        pos = self.get(market_id)
        if pos is None:
            return None
        for k, v in kwargs.items():
            if hasattr(pos, k):
                setattr(pos, k, v)
        self.save()
        return pos

    def has(self, market_id: str) -> bool:
        return any(p.market_id == market_id for p in self.positions)

    # ── P&L computation ─────────────────────────────────────────────

    def update_prices(
        self,
        prices: dict[str, list[float]],
        outcomes: dict[str, list[str]],
    ) -> list[str]:
        """Update all positions with current prices and return new alerts.

        Args:
            prices: market_id -> [yes_price, no_price, ...]
            outcomes: market_id -> ["Yes", "No"] or ["Team A", "Team B", ...]
        """
        alerts: list[str] = []

        for pos in self.positions:
            if pos.market_id not in prices:
                continue

            market_prices = prices[pos.market_id]
            market_outcomes = outcomes.get(pos.market_id, [])

            # Find the price for the side the user holds
            price = self._resolve_price(pos.side, market_prices, market_outcomes)
            if price is None:
                continue

            pos.update_pnl(price)

            # Check profit targets
            if pos.market_id not in self._alerted:
                self._alerted[pos.market_id] = set()

            for target in PROFIT_TARGETS:
                if pos.pnl_pct >= target and target not in self._alerted[pos.market_id]:
                    self._alerted[pos.market_id].add(target)
                    pct = target * 100
                    alerts.append(
                        f"$ PROFIT +{pct:.0f}%: {pos.question[:40]} "
                        f"({pos.side} {pos.entry_price:.2f} -> {pos.current_price:.2f})"
                    )

            # Loss warning (only once)
            if pos.pnl_pct <= LOSS_WARN and LOSS_WARN not in self._alerted[pos.market_id]:
                self._alerted[pos.market_id].add(LOSS_WARN)
                alerts.append(
                    f"! LOSS {pos.pnl_pct:+.0%}: {pos.question[:40]} "
                    f"({pos.side} {pos.entry_price:.2f} -> {pos.current_price:.2f})"
                )

        return alerts

    @staticmethod
    def _resolve_price(
        side: str,
        prices: list[float],
        outcomes: list[str],
    ) -> float | None:
        """Find the current price for the held side."""
        side_lower = side.lower()

        # Try matching by outcome name
        for i, name in enumerate(outcomes):
            if name.lower() == side_lower and i < len(prices):
                return prices[i]

        # Fall back to YES=index 0, NO=index 1
        if side_lower in ("yes", "y") and len(prices) > 0:
            return prices[0]
        if side_lower in ("no", "n") and len(prices) > 1:
            return prices[1]

        # If we have prices but couldn't match, assume index 0
        return prices[0] if prices else None
