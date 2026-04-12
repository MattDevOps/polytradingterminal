"""Scoring engine – orchestrates data fetching and factor computation."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .api import PolymarketAPI
from .factors.divergence import compute_divergence
from .factors.disposition import compute_disposition
from .factors.pairs import PairSignal, compute_pairs
from .factors.velocity import compute_velocity
from .models import FactorScore, Market, MarketScore, Signal
from .notify import send_toast
from .portfolio import Portfolio

log = logging.getLogger(__name__)

# How many top markets (by 24h volume) to analyse in depth
TOP_N = 40

# Markets closing within this many seconds trigger an early warning toast
CLOSING_SOON_SECS = 600  # 10 minutes


@dataclass
class EngineState:
    """Snapshot produced by each engine cycle."""

    markets: list[MarketScore] = field(default_factory=list)
    pair_signals: list[PairSignal] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    cycle: int = 0
    last_refresh: float = 0.0
    error: str | None = None
    closing_soon: bool = False  # True when any actionable market is near close


class Engine:
    """Pulls data, scores every market on four factors, emits state."""

    def __init__(self) -> None:
        self.api = PolymarketAPI()
        self.state = EngineState()
        self._prev_signals: dict[str, Signal] = {}
        self.portfolio = Portfolio()
        self._notified_closing: set[str] = set()  # market IDs already toasted

    async def close(self) -> None:
        await self.api.close()

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    async def refresh(self) -> EngineState:
        t0 = time.monotonic()
        self.state = EngineState(cycle=self.state.cycle + 1)
        try:
            await self._run_cycle()
        except Exception as exc:
            log.exception("Engine cycle failed")
            self.state.error = str(exc)
        self.state.last_refresh = time.monotonic() - t0
        return self.state

    async def _run_cycle(self) -> None:
        # 1. Fetch top markets by 24h volume
        markets = await self.api.get_markets(limit=TOP_N, active=True)
        markets = [
            m for m in markets
            if m.outcome_prices and m.clob_token_ids and not m.closed
        ]

        if not markets:
            self.state.error = "No active markets returned from API"
            return

        log.info("Fetched %d markets", len(markets))

        # 2. Fetch enrichment data in parallel
        price_series, trades_by_market = await self._fetch_enrichment(markets)

        # 3. Compute all four factors in parallel
        div_scores, (disp_scores, vel_scores, (pair_scores, pair_signals)) = (
            await asyncio.gather(
                compute_divergence(markets, price_series),
                asyncio.gather(
                    compute_disposition(markets, trades_by_market, price_series),
                    compute_velocity(markets, trades_by_market),
                    compute_pairs(markets, price_series),
                ),
            )
        )

        self.state.pair_signals = pair_signals

        # 4. Assemble scored markets
        scored: list[MarketScore] = []
        for m in markets:
            ms = MarketScore(
                market=m,
                divergence=div_scores.get(m.id, FactorScore("divergence", 0.0)),
                disposition=disp_scores.get(m.id, FactorScore("disposition", 0.0)),
                velocity=vel_scores.get(m.id, FactorScore("velocity", 0.0)),
                pairs=pair_scores.get(m.id, FactorScore("pairs", 0.0)),
            )
            ms.score()
            scored.append(ms)

        # Sort by composite descending
        scored.sort(key=lambda s: s.composite, reverse=True)
        self.state.markets = scored

        # 5. Generate alerts for signal changes
        for ms in scored:
            prev = self._prev_signals.get(ms.market.id)
            if prev and prev != ms.signal:
                arrow = "▲" if ms.signal in (Signal.ENTER, Signal.STRONG_ENTER) else "▼"
                short_q = ms.market.question[:50]
                self.state.alerts.append(
                    f"{arrow} {ms.signal.value}: {short_q} (was {prev.value})"
                )
            self._prev_signals[ms.market.id] = ms.signal

        # 5b. Closing-soon warnings for actionable markets
        _ACTIONABLE = {Signal.ENTER, Signal.STRONG_ENTER, Signal.HOLD}
        now = datetime.now(timezone.utc)
        for ms in scored:
            end = _parse_end_date(ms.market.end_date)
            if end is None or ms.signal not in _ACTIONABLE:
                continue
            remaining = (end - now).total_seconds()
            if 0 < remaining <= CLOSING_SOON_SECS:
                self.state.closing_soon = True
                if ms.market.id not in self._notified_closing:
                    self._notified_closing.add(ms.market.id)
                    mins = max(1, int(remaining // 60))
                    short_q = ms.market.question[:50]
                    self.state.alerts.append(
                        f"⏰ CLOSING in ~{mins}m: {short_q} [{ms.signal.value}]"
                    )
                    if ms.signal in {Signal.ENTER, Signal.STRONG_ENTER}:
                        slug = ms.market.event_slug or ms.market.slug
                        url = f"https://polymarket.com/event/{slug}" if slug else None
                        send_toast(
                            f"Market closing in ~{mins} min!",
                            f"[{ms.signal.value}] BUY {ms.pick_label} — {short_q}\n"
                            f"Score {ms.composite:.2f}",
                            url=url,
                        )

        # Expire tracking for markets that already closed
        self._notified_closing = {
            mid for mid in self._notified_closing
            if mid in {ms.market.id for ms in scored}
        }

        # Pair divergence alerts
        for ps in pair_signals:
            a_name = _find_name(markets, ps.market_a_id)
            b_name = _find_name(markets, ps.market_b_id)
            self.state.alerts.append(
                f"↔ PAIR z={ps.z_score}: {a_name[:30]} / {b_name[:30]}"
            )

        # 6. Portfolio P&L tracking
        if self.portfolio.positions:
            prices_map: dict[str, list[float]] = {}
            outcomes_map: dict[str, list[str]] = {}
            active_ids = set()
            for m in markets:
                prices_map[m.id] = m.outcome_prices
                outcomes_map[m.id] = m.outcomes
                active_ids.add(m.id)
            pnl_alerts = self.portfolio.update_prices(prices_map, outcomes_map)
            self.state.alerts.extend(pnl_alerts)

            # Check held positions that aren't in the active market list
            # — they may have closed/resolved
            resolution_alerts = await self._check_resolutions(active_ids)
            self.state.alerts.extend(resolution_alerts)

            # Also alert if a held position's signal degrades to EXIT
            for ms in scored:
                pos = self.portfolio.get(ms.market.id)
                if pos and not pos.resolved and ms.signal == Signal.EXIT:
                    msg = (
                        f"! SELL {pos.side}: {pos.question[:40]} "
                        f"— signal EXIT + P&L {pos.pnl_pct:+.0%}"
                    )
                    self.state.alerts.append(msg)
                    slug = ms.market.event_slug or ms.market.slug
                    url = f"https://polymarket.com/event/{slug}" if slug else None
                    send_toast(
                        "EXIT Signal — Consider Selling",
                        f"{pos.question[:50]}\n{pos.side} | P&L {pos.pnl_pct:+.0%}",
                        url=url,
                    )

            # Desktop notifications for profit targets on held positions
            for alert in pnl_alerts:
                if alert.startswith("$ PROFIT"):
                    send_toast("Profit Target Hit", alert)

    # ------------------------------------------------------------------
    # Resolution detection
    # ------------------------------------------------------------------

    async def _check_resolutions(self, active_ids: set[str]) -> list[str]:
        """Check held positions not in the active scan for market closure."""
        alerts: list[str] = []
        unresolved = [
            p for p in self.portfolio.positions
            if not p.resolved and p.market_id not in active_ids
        ]
        if not unresolved:
            return alerts

        # Fetch market data for each missing position
        fetch_tasks = [self.api.get_market(p.market_id) for p in unresolved]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        changed = False
        for pos, result in zip(unresolved, results):
            if isinstance(result, Exception) or result is None:
                continue
            market = result
            if not market.closed:
                # Still open, just not in the top-N — update price
                price = self.portfolio._resolve_price(
                    pos.side, market.outcome_prices, market.outcomes,
                )
                if price is not None:
                    pos.update_pnl(price)
                continue

            # Market is closed — determine if user's side won
            # A resolved market has winning outcome at ~1.0 and losers at ~0.0
            price = self.portfolio._resolve_price(
                pos.side, market.outcome_prices, market.outcomes,
            )
            if price is None:
                continue

            won = price > 0.5
            pos.resolve(won)
            changed = True

            if won:
                pnl_style = "+"
                label = "WIN"
                send_toast(
                    "Position Won!",
                    f"{pos.question[:50]}\n{pos.side} | P&L {pos.pnl_pct:+.0%}",
                )
            else:
                pnl_style = ""
                label = "LOSS"
                send_toast(
                    "Position Lost",
                    f"{pos.question[:50]}\n{pos.side} | P&L {pos.pnl_pct:+.0%}",
                )

            alerts.append(
                f"{'✓' if won else '✗'} {label}: {pos.question[:40]} "
                f"({pos.side} @ {pos.entry_price:.2f} → {pos.current_price:.2f}, "
                f"P&L {pos.pnl_pct:+.0%})"
            )

        if changed:
            self.portfolio.save()

        return alerts

    # ------------------------------------------------------------------
    # Data enrichment
    # ------------------------------------------------------------------

    async def _fetch_enrichment(
        self, markets: list[Market],
    ) -> tuple[dict[str, list[float]], dict[str, list[dict]]]:
        """Fetch price histories, live CLOB midpoints, and recent trades."""

        price_series: dict[str, list[float]] = {}
        trades_by_market: dict[str, list[dict]] = {}

        async def fetch_prices(m: Market) -> None:
            if not m.clob_token_ids:
                return
            try:
                hist = await self.api.get_price_history(
                    m.clob_token_ids[0], interval="max", fidelity=60,
                )
                if hist:
                    price_series[m.id] = [float(h.get("p", 0)) for h in hist]
            except Exception as exc:
                log.debug("Price history failed for %s: %s", m.id, exc)

        async def fetch_trades(m: Market) -> None:
            try:
                data = await self.api.get_trades(market=m.condition_id, limit=200)
                if data:
                    trades_by_market[m.id] = data if isinstance(data, list) else []
            except Exception as exc:
                log.debug("Trades failed for %s: %s", m.id, exc)

        async def fetch_live_price(m: Market) -> None:
            """Fetch real-time midpoint from CLOB and overwrite stale Gamma price."""
            if not m.clob_token_ids:
                return
            try:
                mid = await self.api.get_midpoint(m.clob_token_ids[0])
                if mid and mid > 0:
                    m.outcome_prices[0] = mid
                    # For binary markets, complement the second outcome
                    if len(m.outcome_prices) >= 2:
                        m.outcome_prices[1] = round(1.0 - mid, 4)
            except Exception as exc:
                log.debug("CLOB midpoint failed for %s: %s", m.id, exc)

        # Fire all requests concurrently
        tasks = []
        for m in markets:
            tasks.append(fetch_prices(m))
            tasks.append(fetch_trades(m))
            tasks.append(fetch_live_price(m))

        await asyncio.gather(*tasks, return_exceptions=True)

        return price_series, trades_by_market


def _find_name(markets: list[Market], mid: str) -> str:
    for m in markets:
        if m.id == mid:
            return m.question
    return mid[:12]


def _parse_end_date(raw: str | None) -> datetime | None:
    """Parse ISO-8601 end_date from the Gamma API into a tz-aware datetime."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
