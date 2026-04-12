"""Factor 1 – Cross-Market Divergence.

Detects mispricings by comparing a contract's market price against its
implied fair value derived from related contracts in the same event.

For multi-outcome events (grouped by negRiskMarketID) the probabilities of
all outcomes should sum to 1. Any overround (sum > 1) or underround
(sum < 1) indicates mispricing.

For isolated binary markets we measure the bid-ask spread and price
momentum divergence as a proxy for pricing inefficiency.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ..models import FactorScore, Market

log = logging.getLogger(__name__)


async def compute_divergence(
    markets: list[Market],
    price_series: dict[str, list[float]] | None = None,
) -> dict[str, FactorScore]:
    """Return {market_id: FactorScore} for the divergence factor."""

    scores: dict[str, FactorScore] = {}
    price_series = price_series or {}

    # --- Group by negRiskMarketID (Polymarket's multi-outcome grouping) ---
    by_group: dict[str, list[Market]] = defaultdict(list)
    ungrouped: list[Market] = []

    for m in markets:
        if m.neg_risk_id:
            by_group[m.neg_risk_id].append(m)
        elif m.event_id:
            by_group[f"evt-{m.event_id}"].append(m)
        else:
            ungrouped.append(m)

    # Score multi-outcome groups
    for group_id, group in by_group.items():
        if len(group) >= 2:
            _score_multi_outcome(group, scores)
        else:
            _score_binary(group[0], price_series, scores)

    # Score ungrouped markets
    for m in ungrouped:
        _score_binary(m, price_series, scores)

    # Ensure every market has a score
    for m in markets:
        if m.id not in scores:
            scores[m.id] = FactorScore("divergence", 0.0, details="no data")

    return scores


def _score_multi_outcome(group: list[Market], out: dict[str, FactorScore]) -> None:
    """Score each outcome in a multi-outcome event.

    In a fair market, YES prices across all outcomes should sum to 1.0.
    The "vig" is how far they deviate.  Each outcome's fair value is
    price_i / total, and the divergence is |market - fair|.
    """
    prices: list[tuple[Market, float]] = []
    for m in group:
        p = m.outcome_prices[0] if m.outcome_prices else 0.0
        if p > 0:
            prices.append((m, p))

    if not prices:
        for m in group:
            out[m.id] = FactorScore("divergence", 0.0, details="no price data")
        return

    total = sum(p for _, p in prices)
    overround = total - 1.0

    for m, price in prices:
        fair = price / total if total > 0 else price
        divergence = abs(price - fair)

        # Normalize: 5c divergence is significant, 10c+ is extreme
        normalized = min(1.0, divergence / 0.08)

        # Bias: fair > price → YES underpriced (+), fair < price → YES overpriced (-)
        raw_bias = fair - price
        bias = max(-1.0, min(1.0, raw_bias / 0.08))

        group_name = m.group_title or m.question[:20]
        out[m.id] = FactorScore(
            name="divergence",
            value=round(normalized, 3),
            raw=round(divergence, 4),
            bias=round(bias, 3),
            details=(
                f"[{group_name}] fair {fair:.2f} vs mkt {price:.2f} "
                f"(Δ{divergence:.3f}, vig {overround:+.3f}, {len(prices)} outcomes)"
            ),
        )

    for m in group:
        if m.id not in out:
            out[m.id] = FactorScore("divergence", 0.0, details="no price in group")


def _score_binary(
    m: Market,
    price_series: dict[str, list[float]],
    out: dict[str, FactorScore],
) -> None:
    """Score a binary market using spread + price momentum analysis."""

    components: list[float] = []
    details_parts: list[str] = []

    # 1. Bid-ask spread / vig
    if len(m.outcome_prices) >= 2:
        yes, no = m.outcome_prices[0], m.outcome_prices[1]
        vig = abs(yes + no - 1.0)
        components.append(min(1.0, vig / 0.05))
        details_parts.append(f"vig {vig:.3f}")

    spread = m.spread if m.spread > 0 else (m.best_ask - m.best_bid if m.best_ask > m.best_bid else 0)
    if spread > 0:
        components.append(min(1.0, spread / 0.04))
        details_parts.append(f"spread {spread:.3f}")

    # 2. Price momentum divergence: is the last price far from recent mean?
    bias = 0.0
    prices = price_series.get(m.id, [])
    if len(prices) >= 20:
        recent = prices[-20:]
        mean_price = sum(recent) / len(recent)
        last_price = prices[-1]
        momentum_div = abs(last_price - mean_price)
        components.append(min(1.0, momentum_div / 0.08))
        details_parts.append(f"mom Δ{momentum_div:.3f}")
        # Mean reversion: if price dropped below mean → YES underpriced (+)
        #                  if price rose above mean → YES overpriced (-)
        raw_bias = mean_price - last_price
        bias = max(-1.0, min(1.0, raw_bias / 0.08))

    if not components:
        out[m.id] = FactorScore("divergence", 0.0, details="insufficient data")
        return

    value = sum(components) / len(components)

    out[m.id] = FactorScore(
        name="divergence",
        value=round(value, 3),
        raw=round(components[0] if components else 0, 4),
        bias=round(bias, 3),
        details=", ".join(details_parts),
    )
