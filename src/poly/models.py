from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Signal(Enum):
    STRONG_ENTER = "STRONG ENTER"
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"
    NEUTRAL = "NEUTRAL"


@dataclass
class Market:
    id: str
    question: str
    slug: str
    outcomes: list[str]
    outcome_prices: list[float]
    clob_token_ids: list[str]
    condition_id: str
    volume: float
    liquidity: float
    volume_24h: float
    active: bool
    closed: bool
    end_date: str | None = None
    event_id: str | None = None
    event_slug: str | None = None
    neg_risk_id: str | None = None   # groups multi-outcome markets
    group_title: str | None = None
    spread: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0


@dataclass
class FactorScore:
    name: str
    value: float          # 0.0 – 1.0  (higher = stronger signal)
    raw: float = 0.0      # un-normalized metric for display
    details: str = ""
    bias: float = 0.0     # -1.0 (strong NO) … +1.0 (strong YES)

    @property
    def bar(self) -> str:
        filled = round(self.value * 4)
        return "█" * filled + "░" * (4 - filled)


@dataclass
class MarketScore:
    market: Market
    divergence: FactorScore = field(default_factory=lambda: FactorScore("divergence", 0.0))
    disposition: FactorScore = field(default_factory=lambda: FactorScore("disposition", 0.0))
    velocity: FactorScore = field(default_factory=lambda: FactorScore("velocity", 0.0))
    pairs: FactorScore = field(default_factory=lambda: FactorScore("pairs", 0.0))
    composite: float = 0.0
    signal: Signal = Signal.NEUTRAL

    @property
    def factors(self) -> list[FactorScore]:
        return [self.divergence, self.disposition, self.velocity, self.pairs]

    @property
    def pick(self) -> str:
        """Return 'YES' or 'NO' — the recommended side to bet."""
        # Weighted blend: divergence (mispricing direction) + disposition (smart-money flow)
        div_bias = self.divergence.bias
        disp_bias = self.disposition.bias
        # Both carry directional info; average them
        combined = div_bias * 0.5 + disp_bias * 0.5
        return "YES" if combined >= 0 else "NO"

    @property
    def pick_is_yes(self) -> bool:
        return self.pick == "YES"

    @property
    def pick_label(self) -> str:
        """Human-readable pick — shows outcome name (e.g. team) instead of YES/NO."""
        outcomes = self.market.outcomes
        if self.pick_is_yes:
            name = outcomes[0] if outcomes else "YES"
        else:
            name = outcomes[1] if len(outcomes) > 1 else "NO"
        # Standard binary markets keep YES/NO
        if name.lower() in ("yes", "no"):
            return name.upper()
        return name

    @property
    def pick_confidence(self) -> float:
        """0.0 – 1.0 confidence in the pick direction."""
        div_bias = self.divergence.bias
        disp_bias = self.disposition.bias
        combined = div_bias * 0.5 + disp_bias * 0.5
        return min(1.0, abs(combined))

    def score(self) -> None:
        """Compute composite score and signal from four factors.

        The rule: when all four factors align → ENTER.
        When any factor *actively breaks* (drops well below threshold) → EXIT.
        """
        vals = [f.value for f in self.factors]

        # Composite: blend of average and minimum (penalizes weakness)
        avg = sum(vals) / 4
        self.composite = min(vals) * 0.3 + avg * 0.7

        aligned = sum(1 for v in vals if v >= 0.50)
        strong  = sum(1 for v in vals if v >= 0.65)
        weak    = sum(1 for v in vals if v < 0.20)

        if strong >= 4 and self.composite >= 0.65:
            self.signal = Signal.STRONG_ENTER
        elif aligned >= 3 and strong >= 2 and self.composite >= 0.50:
            self.signal = Signal.ENTER
        elif aligned >= 2 and self.composite >= 0.40:
            self.signal = Signal.HOLD
        elif weak >= 2:
            self.signal = Signal.EXIT
        else:
            self.signal = Signal.NEUTRAL
