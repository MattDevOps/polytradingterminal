"""Factor 4 – Pair Network Correlation.

Builds a correlation network from price return series across all active
markets.  When historically correlated contracts diverge, that's a signal.

    r_ij = corr(returns_i, returns_j)

A pair is "tradable" when:
  |r_ij| > threshold and z-score of recent divergence > 1.5.

Mean-reversion horizon on Polymarket is typically 2-48 hours for
event-linked pairs.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass

import numpy as np

from ..models import FactorScore, Market

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")

CORR_THRESHOLD = 0.45
Z_THRESHOLD    = 1.5
MIN_POINTS     = 12


@dataclass
class PairSignal:
    market_a_id: str
    market_b_id: str
    correlation: float
    z_score: float
    direction: str  # "long_a" or "long_b"


async def compute_pairs(
    markets: list[Market],
    price_series: dict[str, list[float]],
) -> tuple[dict[str, FactorScore], list[PairSignal]]:
    """Return ({market_id: FactorScore}, [PairSignal]).

    The score for each market reflects how many correlated pairs it has
    and whether any are currently diverged (tradable).
    """

    scores: dict[str, FactorScore] = {}
    signals: list[PairSignal] = []

    # --- Compute returns --------------------------------------------------
    returns: dict[str, np.ndarray] = {}
    for mid, prices in price_series.items():
        if len(prices) >= MIN_POINTS:
            arr = np.array(prices, dtype=float)
            # Log returns (handles zeros gracefully)
            with np.errstate(divide="ignore", invalid="ignore"):
                r = np.diff(np.log(np.where(arr > 0, arr, 1e-8)))
            r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
            returns[mid] = r

    market_ids = list(returns.keys())
    n = len(market_ids)

    if n < 2:
        for m in markets:
            scores[m.id] = FactorScore("pairs", 0.0, details="need ≥2 markets with history")
        return scores, signals

    # --- Build correlation matrix -----------------------------------------
    # Align series to common length
    min_len = min(len(returns[mid]) for mid in market_ids)
    if min_len < MIN_POINTS:
        for m in markets:
            scores[m.id] = FactorScore("pairs", 0.0, details="insufficient history")
        return scores, signals

    matrix = np.zeros((n, min_len))
    for i, mid in enumerate(market_ids):
        matrix[i] = returns[mid][-min_len:]

    corr = np.corrcoef(matrix)
    corr = np.nan_to_num(corr, nan=0.0)

    # --- Find correlated pairs and divergences ----------------------------
    pair_count: dict[str, int] = {mid: 0 for mid in market_ids}
    diverged_count: dict[str, int] = {mid: 0 for mid in market_ids}
    best_z: dict[str, float] = {mid: 0.0 for mid in market_ids}

    for i in range(n):
        for j in range(i + 1, n):
            r = corr[i, j]
            if abs(r) < CORR_THRESHOLD:
                continue

            mid_a = market_ids[i]
            mid_b = market_ids[j]
            pair_count[mid_a] += 1
            pair_count[mid_b] += 1

            # Check for divergence: z-score of recent spread
            spread = matrix[i] - (r * matrix[j])  # residual
            if len(spread) >= 10:
                lookback = spread[-20:]
                recent   = spread[-5:]
                mu  = np.mean(lookback)
                std = np.std(lookback)
                if std > 1e-8:
                    z = abs(np.mean(recent) - mu) / std
                else:
                    z = 0.0

                if z >= Z_THRESHOLD:
                    diverged_count[mid_a] += 1
                    diverged_count[mid_b] += 1
                    best_z[mid_a] = max(best_z[mid_a], z)
                    best_z[mid_b] = max(best_z[mid_b], z)

                    direction = "long_a" if np.mean(recent) < mu else "long_b"
                    signals.append(PairSignal(
                        market_a_id=mid_a,
                        market_b_id=mid_b,
                        correlation=round(float(r), 3),
                        z_score=round(float(z), 2),
                        direction=direction,
                    ))

    # --- Compute per-market score -----------------------------------------
    max_pairs = max(pair_count.values()) if pair_count else 1

    for m in markets:
        mid = m.id
        if mid not in pair_count:
            # No price history at all → baseline score
            scores[mid] = FactorScore("pairs", 0.15, details="no price history")
            continue

        pc = pair_count[mid]
        dc = diverged_count[mid]
        z  = best_z[mid]

        # Base score: having analyzable history is worth something
        base = 0.25

        # Connectivity bonus (more correlated pairs = richer network)
        conn = min(0.35, (pc / max(max_pairs, 1)) * 0.35)

        # Divergence bonus (active mispricing opportunity = strongest signal)
        div = min(0.40, (z / 3.0) * 0.40) if dc > 0 else 0.0

        value = min(1.0, base + conn + div)

        scores[mid] = FactorScore(
            name="pairs",
            value=round(value, 3),
            raw=round(z, 2),
            details=f"{pc} pairs, {dc} diverged, z={z:.1f}",
        )

    return scores, signals
