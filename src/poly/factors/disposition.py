"""Factor 2 – Disposition Coefficient.

Measures *how* wallets exit, not how they enter.

For each wallet's round-trip on a market:
  WinnerCapture = (exit_price - entry_price) / (peak_price - entry_price)
  LoserHoldRatio = (entry_price - exit_price) / (entry_price - trough_price)
  DC = WinnerCapture / LoserHoldRatio     (higher = better exits)

Elite wallets: DC ~ 7  (capture 86% of gains, cut losers at 12%)
Average:       DC ~ 1.4 (capture 58%, hold losers to 41%)

As a market-level signal we ask: are the wallets currently entering this
market the ones with good exit discipline?

Polymarket trade format:
  proxyWallet, side (BUY/SELL), asset, price, size, timestamp
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ..models import FactorScore, Market

log = logging.getLogger(__name__)


async def compute_disposition(
    markets: list[Market],
    trades_by_market: dict[str, list[dict]],
    price_series: dict[str, list[float]],
) -> dict[str, FactorScore]:
    """Return {market_id: FactorScore} for the disposition factor."""

    scores: dict[str, FactorScore] = {}
    for m in markets:
        trades = trades_by_market.get(m.id, [])
        prices = price_series.get(m.id, [])
        scores[m.id] = _score_market(m, trades, prices)
    return scores


def _score_market(
    market: Market, trades: list[dict], prices: list[float],
) -> FactorScore:
    if len(trades) < 5:
        return _fallback(market)

    # Group by wallet
    wallets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        addr = t.get("proxyWallet", "")
        if addr:
            wallets[addr].append(t)

    if not wallets:
        return _fallback(market)

    peak = max(prices) if prices else 1.0
    trough = min(prices) if prices else 0.0
    price_range = peak - trough if peak > trough else 0.01

    wallet_dcs: list[float] = []
    wallet_vols: list[float] = []

    for addr, wtrades in wallets.items():
        buys  = [t for t in wtrades if str(t.get("side", "")).upper() == "BUY"]
        sells = [t for t in wtrades if str(t.get("side", "")).upper() == "SELL"]

        if not buys or not sells:
            continue

        avg_entry = _vwap(buys)
        avg_exit  = _vwap(sells)
        vol = sum(_f(t.get("size", 0)) * _f(t.get("price", 0)) for t in wtrades)

        if avg_entry <= 0 or avg_exit <= 0:
            continue

        if avg_exit > avg_entry:
            # Winner: how much of the available gain did they capture?
            gain_frac = (avg_exit - avg_entry) / price_range
            dc = min(gain_frac * 8, 10.0)
        else:
            # Loser: how deep did they hold before cutting?
            loss_frac = (avg_entry - avg_exit) / price_range
            dc = max(1.0 - loss_frac * 4, 0.0)

        wallet_dcs.append(dc)
        wallet_vols.append(vol)

    if not wallet_dcs:
        return _fallback(market)

    total_vol = sum(wallet_vols) or 1.0
    weighted_dc = sum(d * v for d, v in zip(wallet_dcs, wallet_vols)) / total_vol

    # Normalize: DC 5+ is elite, 1 is average, 0 is worst
    dc_score = min(1.0, weighted_dc / 5.0)

    # Recent flow bias: net buying or selling?
    recent = trades[-min(50, len(trades)):]
    buy_vol  = sum(_f(t.get("size", 0)) for t in recent if str(t.get("side", "")).upper() == "BUY")
    sell_vol = sum(_f(t.get("size", 0)) for t in recent if str(t.get("side", "")).upper() == "SELL")
    total_r = buy_vol + sell_vol or 1.0
    flow = buy_vol / total_r  # >0.5 = net buying

    blended = dc_score * 0.65 + flow * 0.35

    return FactorScore(
        name="disposition",
        value=round(min(1.0, blended), 3),
        raw=round(weighted_dc, 2),
        details=f"DC {weighted_dc:.1f}, flow {flow:.0%} buy, {len(wallet_dcs)} wallets",
    )


def _fallback(market: Market) -> FactorScore:
    """Estimate from volume/liquidity when trade-level data is sparse."""
    if market.volume_24h > 0 and market.liquidity > 0:
        ratio = market.volume_24h / market.liquidity
        score = min(1.0, ratio / 8.0)
    else:
        score = 0.3
    return FactorScore(
        "disposition", round(score, 3), 0.0,
        "estimated (limited trade data)",
    )


def _vwap(trades: list[dict]) -> float:
    notional = sum(_f(t.get("price", 0)) * _f(t.get("size", 0)) for t in trades)
    size = sum(_f(t.get("size", 0)) for t in trades)
    return notional / size if size > 0 else 0.0


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (ValueError, TypeError):
        return 0.0
