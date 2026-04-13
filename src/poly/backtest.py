"""Backtest – replay the scoring engine on resolved markets to evaluate thresholds."""

from __future__ import annotations

import asyncio
import csv
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from copy import deepcopy

from .api import PolymarketAPI
from .factors.divergence import compute_divergence
from .factors.disposition import compute_disposition
from .factors.pairs import compute_pairs
from .factors.velocity import compute_velocity
from .models import FactorScore, Market, MarketScore, Signal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ResolvedMarket:
    market: Market
    ground_truth_yes: bool          # True if YES outcome won
    price_history: list[dict]       # Raw [{t: ..., p: ...}, ...]
    trades: list[dict]
    t_start: float = 0.0
    t_end: float = 0.0


@dataclass
class Snapshot:
    market_id: str
    fraction: float
    t_cutoff: float
    price_series: list[float]
    trades: list[dict]
    entry_price: float
    n_prices: int = 0
    n_trades: int = 0


@dataclass
class SnapshotResult:
    market_id: str
    question: str
    fraction: float
    ground_truth_yes: bool
    entry_price: float
    predicted_yes: bool
    pick_confidence: float
    divergence: float
    disposition: float
    velocity: float
    pairs: float
    composite: float
    signal: Signal
    n_prices: int
    n_trades: int
    profit: float


@dataclass
class ThresholdResult:
    strong_threshold: float
    n_signals: int
    n_correct: int
    accuracy: float
    avg_profit: float
    total_profit: float
    win_rate: float
    sharpe: float


# ---------------------------------------------------------------------------
# 1. Data collection
# ---------------------------------------------------------------------------

def _determine_ground_truth(market: Market) -> bool | None:
    """Return True if YES won, False if NO won, None if ambiguous."""
    if len(market.outcome_prices) < 2:
        return None
    yes_p = market.outcome_prices[0]
    no_p = market.outcome_prices[1]
    if yes_p > 0.9:
        return True
    if no_p > 0.9:
        return False
    return None


async def _enrich_market(
    api: PolymarketAPI, market: Market,
) -> ResolvedMarket | None:
    """Fetch price history + trades for one resolved market."""
    gt = _determine_ground_truth(market)
    if gt is None:
        return None

    token_id = market.clob_token_ids[0] if market.clob_token_ids else None
    if not token_id:
        return None

    try:
        hist, trades = await asyncio.gather(
            api.get_price_history(token_id, interval="max", fidelity=60),
            api.get_trades(market=market.condition_id, limit=500),
        )
    except Exception as exc:
        log.debug("Enrich failed for %s: %s", market.id, exc)
        return None

    if not hist or len(hist) < 30:
        return None

    timestamps = [float(h.get("t", 0)) for h in hist]
    if not timestamps or max(timestamps) == 0:
        return None

    return ResolvedMarket(
        market=market,
        ground_truth_yes=gt,
        price_history=hist,
        trades=trades if isinstance(trades, list) else [],
        t_start=min(timestamps),
        t_end=max(timestamps),
    )


async def fetch_resolved_markets(
    api: PolymarketAPI, target_count: int = 250, batch_size: int = 100,
) -> list[ResolvedMarket]:
    """Fetch closed binary markets and enrich with price/trade data."""
    raw_markets: list[Market] = []
    offset = 0
    max_pages = 10

    print("Fetching resolved markets from API...")
    for page in range(max_pages):
        batch = await api.get_markets(
            limit=batch_size, offset=offset, active=False, closed=True,
            order="volume24hr", ascending=False,
        )
        if not batch:
            break
        # Filter to binary markets with clear resolution and real volume
        for m in batch:
            if (
                len(m.outcomes) == 2
                and m.clob_token_ids
                and m.volume > 1000
                and _determine_ground_truth(m) is not None
            ):
                raw_markets.append(m)
        offset += batch_size
        sys.stdout.write(f"\r  Fetched page {page + 1}, {len(raw_markets)} qualifying markets so far...")
        sys.stdout.flush()
        if len(raw_markets) >= target_count:
            break

    raw_markets = raw_markets[:target_count]
    print(f"\n  {len(raw_markets)} binary resolved markets found")

    # Enrich in parallel
    print("Enriching with price history and trades...")
    tasks = [_enrich_market(api, m) for m in raw_markets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    resolved: list[ResolvedMarket] = []
    for r in results:
        if isinstance(r, ResolvedMarket):
            resolved.append(r)

    print(f"  {len(resolved)} markets enriched ({len(raw_markets) - len(resolved)} skipped: insufficient data)")
    return resolved


# ---------------------------------------------------------------------------
# 2. Snapshot construction
# ---------------------------------------------------------------------------

def build_snapshots(
    rm: ResolvedMarket, fractions: list[float],
) -> list[Snapshot]:
    """Create truncated data views at each fraction of the market's lifetime.

    Price series are truncated to the snapshot point (so divergence/pairs
    can't peek at the resolution).  Trades are passed through in full —
    the API only returns the most recent 500, which for resolved markets
    all cluster near the end.  Filtering them by cutoff time would leave
    almost none, making disposition/velocity useless.  Using all available
    trades is an acceptable compromise: the factors evaluate *how* wallets
    trade (exit discipline, capital velocity), not *when*.
    """
    span = rm.t_end - rm.t_start
    if span <= 0:
        return []

    snapshots: list[Snapshot] = []
    for frac in fractions:
        t_cutoff = rm.t_start + frac * span

        truncated_hist = [h for h in rm.price_history if float(h.get("t", 0)) <= t_cutoff]
        if len(truncated_hist) < 20:
            continue

        price_series = [float(h.get("p", 0)) for h in truncated_hist]
        entry_price = price_series[-1] if price_series else 0.5

        snapshots.append(Snapshot(
            market_id=rm.market.id,
            fraction=frac,
            t_cutoff=t_cutoff,
            price_series=price_series,
            trades=rm.trades,       # all available trades (not time-filtered)
            entry_price=entry_price,
            n_prices=len(price_series),
            n_trades=len(rm.trades),
        ))

    return snapshots


def build_market_at_snapshot(rm: ResolvedMarket, snap: Snapshot) -> Market:
    """Create a Market reflecting state at snapshot time."""
    m = deepcopy(rm.market)
    m.outcome_prices = [snap.entry_price, round(1.0 - snap.entry_price, 4)]
    m.closed = False
    m.active = True

    # Estimate volume/liquidity from all available trade data.
    # The original market metadata is post-resolution and stale, so we
    # derive realistic proxies from the actual trade activity.
    if snap.trades:
        total_notional = sum(
            _f(t.get("price", 0)) * _f(t.get("size", 0)) for t in snap.trades
        )
        # Approximate 24h volume as total notional / estimated market days
        timestamps = [_f(t.get("timestamp", 0)) for t in snap.trades if _f(t.get("timestamp", 0)) > 0]
        if len(timestamps) >= 2:
            span_days = max((max(timestamps) - min(timestamps)) / 86400, 1.0)
            m.volume_24h = max(total_notional / span_days, 500.0)
        else:
            m.volume_24h = max(total_notional, 500.0)
        # Liquidity: use original if it looks reasonable, otherwise estimate
        if rm.market.liquidity > 100:
            m.liquidity = rm.market.liquidity
        else:
            m.liquidity = max(m.volume_24h / 3.0, 1000.0)
    else:
        # Use original market values as-is if no trades
        m.volume_24h = max(rm.market.volume_24h, 500.0)
        m.liquidity = max(rm.market.liquidity, 1000.0)

    # Estimate spread from recent price movement
    if len(snap.price_series) >= 10:
        recent_prices = snap.price_series[-10:]
        price_range = max(recent_prices) - min(recent_prices)
        m.spread = max(price_range, 0.005)
        m.best_bid = max(0.01, snap.entry_price - m.spread / 2)
        m.best_ask = min(0.99, snap.entry_price + m.spread / 2)
    else:
        m.spread = 0.02
        m.best_bid = max(0.01, snap.entry_price - 0.01)
        m.best_ask = min(0.99, snap.entry_price + 0.01)

    return m


# ---------------------------------------------------------------------------
# 3. Factor replay
# ---------------------------------------------------------------------------

def _compute_profit(predicted_yes: bool, ground_truth_yes: bool, entry_price_yes: float) -> float:
    if predicted_yes:
        cost = entry_price_yes
        payout = 1.0 if ground_truth_yes else 0.0
    else:
        cost = 1.0 - entry_price_yes
        payout = 1.0 if not ground_truth_yes else 0.0
    return payout - cost


async def score_snapshot_batch(
    batch: list[tuple[ResolvedMarket, Snapshot]],
) -> list[SnapshotResult]:
    """Score a batch of snapshots through the factor pipeline."""
    if not batch:
        return []

    # Build inputs for factor functions
    markets: list[Market] = []
    price_series: dict[str, list[float]] = {}
    trades_by_market: dict[str, list[dict]] = {}
    rm_lookup: dict[str, ResolvedMarket] = {}
    snap_lookup: dict[str, Snapshot] = {}

    for rm, snap in batch:
        m = build_market_at_snapshot(rm, snap)
        markets.append(m)
        price_series[m.id] = snap.price_series
        trades_by_market[m.id] = snap.trades
        rm_lookup[m.id] = rm
        snap_lookup[m.id] = snap

    # Run all four factors
    div_scores, disp_scores, vel_scores, (pair_scores, _) = await asyncio.gather(
        compute_divergence(markets, price_series),
        compute_disposition(markets, trades_by_market, price_series),
        compute_velocity(markets, trades_by_market),
        compute_pairs(markets, price_series),
    )

    # Assemble results
    results: list[SnapshotResult] = []
    for m in markets:
        ms = MarketScore(
            market=m,
            divergence=div_scores.get(m.id, FactorScore("divergence", 0.0)),
            disposition=disp_scores.get(m.id, FactorScore("disposition", 0.0)),
            velocity=vel_scores.get(m.id, FactorScore("velocity", 0.0)),
            pairs=pair_scores.get(m.id, FactorScore("pairs", 0.0)),
        )
        ms.score()

        rm = rm_lookup[m.id]
        snap = snap_lookup[m.id]

        profit = _compute_profit(ms.pick_is_yes, rm.ground_truth_yes, snap.entry_price)

        results.append(SnapshotResult(
            market_id=m.id,
            question=rm.market.question,
            fraction=snap.fraction,
            ground_truth_yes=rm.ground_truth_yes,
            entry_price=snap.entry_price,
            predicted_yes=ms.pick_is_yes,
            pick_confidence=ms.pick_confidence,
            divergence=ms.divergence.value,
            disposition=ms.disposition.value,
            velocity=ms.velocity.value,
            pairs=ms.pairs.value,
            composite=ms.composite,
            signal=ms.signal,
            n_prices=snap.n_prices,
            n_trades=snap.n_trades,
            profit=profit,
        ))

    return results


# ---------------------------------------------------------------------------
# 4. Threshold sweep
# ---------------------------------------------------------------------------

def _reclassify(vals: list[float], composite: float, conf: float,
                comp_floor: float, conf_gate: float) -> Signal:
    """Re-apply signal logic with custom composite floor and confidence gate."""
    aligned = sum(1 for v in vals if v >= 0.50)
    weak = sum(1 for v in vals if v < 0.20)

    if composite >= comp_floor + 0.05 and conf >= conf_gate + 0.1 and aligned >= 3:
        return Signal.STRONG_ENTER
    elif composite >= comp_floor and conf >= conf_gate:
        return Signal.ENTER
    elif composite >= comp_floor - 0.05 and aligned >= 2:
        return Signal.HOLD
    elif weak >= 2:
        return Signal.EXIT
    else:
        return Signal.NEUTRAL


def sweep_thresholds(
    results: list[SnapshotResult],
) -> list[ThresholdResult]:
    """Test different composite floor / confidence gate combinations."""
    out: list[ThresholdResult] = []

    configs = [
        (0.25, 0.3),
        (0.30, 0.3),
        (0.30, 0.4),
        (0.35, 0.3),
        (0.35, 0.4),   # ← current
        (0.35, 0.5),
        (0.40, 0.3),
        (0.40, 0.4),
        (0.40, 0.5),
        (0.45, 0.4),
        (0.50, 0.4),
    ]

    for comp_floor, conf_gate in configs:
        profits: list[float] = []
        n_correct = 0

        for r in results:
            vals = [r.divergence, r.disposition, r.velocity, r.pairs]
            sig = _reclassify(vals, r.composite, r.pick_confidence,
                              comp_floor, conf_gate)
            if sig not in (Signal.STRONG_ENTER, Signal.ENTER):
                continue

            profits.append(r.profit)
            correct = r.predicted_yes == r.ground_truth_yes
            if correct:
                n_correct += 1

        n_signals = len(profits)
        if n_signals == 0:
            out.append(ThresholdResult(
                strong_threshold=comp_floor + conf_gate,  # combined label
                n_signals=0, n_correct=0, accuracy=0.0,
                avg_profit=0.0, total_profit=0.0, win_rate=0.0, sharpe=0.0,
            ))
        else:
            accuracy = n_correct / n_signals
            avg_profit = sum(profits) / n_signals
            total_profit = sum(profits)
            win_rate = sum(1 for p in profits if p > 0) / n_signals
            std = statistics.stdev(profits) if n_signals > 1 else 1.0
            sharpe = avg_profit / std if std > 0 else 0.0

            out.append(ThresholdResult(
                strong_threshold=round(comp_floor + conf_gate, 2),
                n_signals=n_signals,
                n_correct=n_correct,
                accuracy=round(accuracy, 4),
                avg_profit=round(avg_profit, 4),
                total_profit=round(total_profit, 2),
                win_rate=round(win_rate, 4),
                sharpe=round(sharpe, 3),
            ))

    return out


# ---------------------------------------------------------------------------
# 5. Reporting
# ---------------------------------------------------------------------------

def print_report(
    threshold_results: list[ThresholdResult],
    snapshot_results: list[SnapshotResult],
    elapsed: float,
    configs: list[tuple[float, float]] | None = None,
) -> None:
    """Print a formatted backtest report."""

    n_markets = len({r.market_id for r in snapshot_results})
    n_snaps = len(snapshot_results)

    print()
    print("=" * 80)
    print("  POLYMARKET BACKTEST RESULTS")
    print("=" * 80)
    print(f"  Markets: {n_markets}  |  Snapshots: {n_snaps}  |  Runtime: {elapsed:.1f}s")
    print()

    # Direction accuracy (all snapshots)
    all_correct = sum(1 for r in snapshot_results if r.predicted_yes == r.ground_truth_yes)
    print(f"  Overall direction accuracy (all snapshots): {all_correct}/{n_snaps} = {all_correct/max(n_snaps,1):.1%}")
    print()

    # Threshold sweep table
    configs = configs or [
        (0.25, 0.3), (0.30, 0.3), (0.30, 0.4), (0.35, 0.3),
        (0.35, 0.4), (0.35, 0.5), (0.40, 0.3), (0.40, 0.4),
        (0.40, 0.5), (0.45, 0.4), (0.50, 0.4),
    ]
    print("  THRESHOLD SWEEP (ENTER + STRONG ENTER signals):")
    print("  " + "-" * 86)
    print(f"  {'Comp':>5} {'Conf':>5}  {'Signals':>7}  {'Correct':>7}  {'Accuracy':>8}  {'Win Rate':>8}  {'Avg P&L':>8}  {'Total P&L':>9}  {'Sharpe':>6}")
    print("  " + "-" * 86)

    best_acc = max((t for t in threshold_results if t.n_signals >= 10), key=lambda t: t.accuracy, default=None)
    best_total = max((t for t in threshold_results if t.n_signals >= 5), key=lambda t: t.total_profit, default=None)

    for t, (cf, cg) in zip(threshold_results, configs):
        marker = ""
        if cf == 0.35 and cg == 0.4:
            marker = "  <-- current"
        elif best_acc and t is best_acc:
            marker = "  <-- best accuracy"
        elif best_total and t is best_total and not (cf == 0.35 and cg == 0.4):
            marker = "  <-- best total"

        print(
            f"  {cf:5.2f} {cg:5.1f}  {t.n_signals:7d}  {t.n_correct:7d}"
            f"  {t.accuracy:7.1%}  {t.win_rate:7.1%}"
            f"  {t.avg_profit:+8.4f}  {t.total_profit:+9.2f}"
            f"  {t.sharpe:6.3f}{marker}"
        )

    print("  " + "-" * 86)

    # Recommendations
    print()
    print("  RECOMMENDATIONS:")
    if best_acc and best_acc.n_signals >= 10:
        print(f"    Best accuracy (10+ signals): {best_acc.accuracy:.1%} "
              f"({best_acc.n_signals} signals, {best_acc.total_profit:+.2f} total P&L)")
    if best_total and best_total.n_signals >= 5:
        print(f"    Best total profit:           {best_total.total_profit:+.2f} "
              f"({best_total.n_signals} signals, {best_total.accuracy:.1%} accuracy)")

    # Find current
    current_idx = next((i for i, (cf, cg) in enumerate(configs) if cf == 0.35 and cg == 0.4), None)
    if current_idx is not None:
        current = threshold_results[current_idx]
        print(f"    Current (comp>=0.35+conf>=0.4): {current.n_signals} signals, "
              f"{current.accuracy:.1%} accuracy, {current.total_profit:+.2f} total profit")

    # Breakdown by fraction
    print()
    print("  BY SNAPSHOT FRACTION:")
    for frac in [0.25, 0.50, 0.75]:
        subset = [r for r in snapshot_results if r.fraction == frac]
        if not subset:
            continue
        correct = sum(1 for r in subset if r.predicted_yes == r.ground_truth_yes)
        avg_p = sum(r.profit for r in subset) / len(subset)
        avg_trades = sum(r.n_trades for r in subset) / len(subset)
        print(f"    {frac:.0%} ({['early', 'mid', 'late'][int(frac*4-1)]:>5}):  "
              f"{len(subset):>4} snapshots, {correct/len(subset):.1%} direction accuracy, "
              f"{avg_p:+.4f} avg profit, {avg_trades:.0f} avg trades")

    # Confusion matrix for current config
    if current_idx is not None:
        current = threshold_results[current_idx]
        if current.n_signals > 0:
            enter_results = [
                r for r in snapshot_results
                if _reclassify(
                    [r.divergence, r.disposition, r.velocity, r.pairs],
                    r.composite, r.pick_confidence, 0.35, 0.4,
                ) in (Signal.STRONG_ENTER, Signal.ENTER)
            ]
            if enter_results:
                tp = sum(1 for r in enter_results if r.predicted_yes and r.ground_truth_yes)
                fp = sum(1 for r in enter_results if r.predicted_yes and not r.ground_truth_yes)
                fn = sum(1 for r in enter_results if not r.predicted_yes and r.ground_truth_yes)
                tn = sum(1 for r in enter_results if not r.predicted_yes and not r.ground_truth_yes)
                print()
                print("  CONFUSION MATRIX (current settings, ENTER signals only):")
                print(f"                      Predicted YES  Predicted NO")
                print(f"    Actual YES    {tp:>13}  {fn:>12}")
                print(f"    Actual NO     {fp:>13}  {tn:>12}")

    # Caveats
    print()
    print("  CAVEATS:")
    print("    - Trade data limited to last 500 per market (early snapshots have fewer trades)")
    print("    - Market metadata (volume, liquidity) estimated from available trade data")
    print("    - Pairs factor may underperform vs live usage (resolved markets less temporally aligned)")
    print()
    print("=" * 80)


def export_csv(results: list[SnapshotResult], path: str = "backtest_results.csv") -> None:
    """Export per-snapshot results to CSV."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "market_id", "question", "fraction", "ground_truth_yes",
            "entry_price", "predicted_yes", "pick_confidence",
            "divergence", "disposition", "velocity", "pairs",
            "composite", "signal", "n_prices", "n_trades", "profit",
        ])
        for r in results:
            writer.writerow([
                r.market_id, r.question, r.fraction, r.ground_truth_yes,
                r.entry_price, r.predicted_yes, r.pick_confidence,
                r.divergence, r.disposition, r.velocity, r.pairs,
                r.composite, r.signal.value, r.n_prices, r.n_trades, r.profit,
            ])
    print(f"  Results exported to {path}")


# ---------------------------------------------------------------------------
# 6. Main entry point
# ---------------------------------------------------------------------------

async def run_backtest(
    target_markets: int = 250,
    fractions: list[float] | None = None,
) -> None:
    """Main backtest orchestrator."""
    if fractions is None:
        fractions = [0.25, 0.50, 0.75]

    t0 = time.monotonic()
    api = PolymarketAPI()

    try:
        # 1. Fetch resolved markets
        resolved = await fetch_resolved_markets(api, target_count=target_markets)
        if not resolved:
            print("No resolved markets found. Cannot run backtest.")
            return

        # 2. Build snapshots
        print("Building snapshots...")
        all_snapshots: list[tuple[ResolvedMarket, Snapshot]] = []
        for rm in resolved:
            for snap in build_snapshots(rm, fractions):
                all_snapshots.append((rm, snap))
        print(f"  {len(all_snapshots)} snapshots across {len(resolved)} markets")

        if not all_snapshots:
            print("No valid snapshots could be constructed.")
            return

        # 3. Score snapshots grouped by fraction (for pairs correlation)
        all_results: list[SnapshotResult] = []
        for frac in fractions:
            batch = [(rm, s) for rm, s in all_snapshots if s.fraction == frac]
            if not batch:
                continue
            sys.stdout.write(f"  Scoring {len(batch)} snapshots at {frac:.0%}...")
            sys.stdout.flush()
            t1 = time.monotonic()
            results = await score_snapshot_batch(batch)
            all_results.extend(results)
            print(f" done ({time.monotonic() - t1:.1f}s)")

        # 4. Sweep thresholds
        threshold_results = sweep_thresholds(all_results)

        # 5. Report
        elapsed = time.monotonic() - t0
        print_report(threshold_results, all_results, elapsed)
        export_csv(all_results)

    finally:
        await api.close()


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (ValueError, TypeError):
        return 0.0
