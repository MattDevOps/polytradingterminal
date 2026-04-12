"""Factor 3 – Capital Velocity.

Measures how fast capital recycles through a market.

    Velocity = TradingVolume / AverageOpenInterest

Top wallets recycle every dollar 49x before average traders recycle once.
High velocity + profitability = active harvesting of mispricings.

We blend:
  • Turnover ratio  (24h volume / liquidity)
  • Trade frequency (trades per hour from recent data)
  • Wallet concentration (are few wallets driving the volume?)
"""

from __future__ import annotations

import logging
from collections import Counter

from ..models import FactorScore, Market

log = logging.getLogger(__name__)


async def compute_velocity(
    markets: list[Market],
    trades_by_market: dict[str, list[dict]],
) -> dict[str, FactorScore]:
    """Return {market_id: FactorScore} for the velocity factor."""
    scores: dict[str, FactorScore] = {}
    for m in markets:
        trades = trades_by_market.get(m.id, [])
        scores[m.id] = _score_market(m, trades)
    return scores


def _score_market(market: Market, trades: list[dict]) -> FactorScore:

    # --- Turnover: 24h volume / liquidity ---------------------------------
    if market.liquidity > 0 and market.volume_24h > 0:
        turnover = market.volume_24h / market.liquidity
    elif market.liquidity > 0 and market.volume > 0:
        turnover = market.volume / market.liquidity / 30
    else:
        return FactorScore("velocity", 0.2, 0.0, "no liquidity data")

    # --- Trade frequency & wallet diversity from recent trades ------------
    tph = 0.0
    wallet_diversity = 0.5  # default neutral

    if len(trades) >= 5:
        timestamps = []
        wallet_counts: Counter[str] = Counter()

        for t in trades:
            ts = _f(t.get("timestamp", 0))
            if ts > 0:
                timestamps.append(ts)
            addr = t.get("proxyWallet", "")
            if addr:
                wallet_counts[addr] += 1

        if len(timestamps) >= 2:
            timestamps.sort()
            span_hrs = max((timestamps[-1] - timestamps[0]) / 3600, 0.5)
            tph = len(trades) / span_hrs

        # Wallet diversity: many unique wallets = healthier market
        if wallet_counts:
            n_wallets = len(wallet_counts)
            n_trades = sum(wallet_counts.values())
            # 1.0 = every trade from a different wallet; 0.0 = one whale
            wallet_diversity = min(1.0, n_wallets / max(n_trades * 0.5, 1))

    # --- Combine ----------------------------------------------------------
    # Turnover: 2x daily is moderate, 8x+ is high velocity
    turnover_score = min(1.0, turnover / 8.0)

    # Frequency: 20 trades/hr is moderate, 100+ is high
    freq_score = min(1.0, tph / 100.0) if tph > 0 else 0.0

    # Blend
    value = turnover_score * 0.50 + freq_score * 0.25 + wallet_diversity * 0.25

    return FactorScore(
        name="velocity",
        value=round(value, 3),
        raw=round(turnover, 2),
        details=f"turnover {turnover:.1f}x, {tph:.0f} tph, {wallet_diversity:.0%} diverse",
    )


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (ValueError, TypeError):
        return 0.0
