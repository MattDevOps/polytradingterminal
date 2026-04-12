"""Scoring engine – orchestrates data fetching and factor computation."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .api import PolymarketAPI
from .factors.divergence import compute_divergence
from .factors.disposition import compute_disposition
from .factors.pairs import PairSignal, compute_pairs
from .factors.velocity import compute_velocity
from .models import FactorScore, Market, MarketScore, Signal
from .portfolio import Portfolio

log = logging.getLogger(__name__)

# How many top markets (by 24h volume) to analyse in depth
TOP_N = 40


@dataclass
class EngineState:
    """Snapshot produced by each engine cycle."""

    markets: list[MarketScore] = field(default_factory=list)
    pair_signals: list[PairSignal] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    cycle: int = 0
    last_refresh: float = 0.0
    error: str | None = None


class Engine:
    """Pulls data, scores every market on four factors, emits state."""

    def __init__(self) -> None:
        self.api = PolymarketAPI()
        self.state = EngineState()
        self._prev_signals: dict[str, Signal] = {}
        self.portfolio = Portfolio()

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
        markets = [m for m in markets if m.outcome_prices and m.clob_token_ids]

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
            for m in markets:
                prices_map[m.id] = m.outcome_prices
                outcomes_map[m.id] = m.outcomes
            pnl_alerts = self.portfolio.update_prices(prices_map, outcomes_map)
            self.state.alerts.extend(pnl_alerts)

            # Also alert if a held position's signal degrades to EXIT
            for ms in scored:
                pos = self.portfolio.get(ms.market.id)
                if pos and ms.signal == Signal.EXIT:
                    self.state.alerts.append(
                        f"! SELL {pos.side}: {pos.question[:40]} "
                        f"— signal EXIT + P&L {pos.pnl_pct:+.0%}"
                    )

    # ------------------------------------------------------------------
    # Data enrichment
    # ------------------------------------------------------------------

    async def _fetch_enrichment(
        self, markets: list[Market],
    ) -> tuple[dict[str, list[float]], dict[str, list[dict]]]:
        """Fetch price histories and recent trades for each market."""

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

        # Fire all requests concurrently
        tasks = []
        for m in markets:
            tasks.append(fetch_prices(m))
            tasks.append(fetch_trades(m))

        await asyncio.gather(*tasks, return_exceptions=True)

        return price_series, trades_by_market


def _find_name(markets: list[Market], mid: str) -> str:
    for m in markets:
        if m.id == mid:
            return m.question
    return mid[:12]
