"""Market regime — the environment a signal is trading into.

The news pipeline answers "is this stock mispriced". This answers a different
question: "what kind of market am I asking that in". The two are kept strictly
separate. Regime never creates a trade — a fearful market is not a reason to do
anything in particular — and it never fully vetoes one. It scales conviction.

Factors, all from instruments already on Alpaca:

  vol_term      VXX / VXZ    short-dated vol above mid-dated = backwardation,
                             the classic signature of genuine stress
  risk_appetite SPLV / SPHB  low-vol outperforming high-beta = defensive tape
  credit        LQD / HYG    investment-grade outperforming junk = credit stress
  breadth       share of the universe above its own 20-day mean, inverted

Every factor is scored as the percentile of today's reading within its own
trailing window. That is the whole trick: no factor carries a hand-chosen
threshold, because "high for this market" is defined by the market itself. The
composite is the mean of whatever is available; missing factors drop out rather
than defaulting, and no factors at all means regime is unknown — reported as
such, never as 0.5 pretending to be knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass

FACTOR_PAIRS = {
    "vol_term": ("VXX", "VXZ"),
    "risk_appetite": ("SPLV", "SPHB"),
    "credit": ("LQD", "HYG"),
}


@dataclass(frozen=True, slots=True)
class RegimeReading:
    score: float | None  # 0 calm .. 1 stressed, None = unknown
    factors: dict
    detail: str

    @property
    def known(self) -> bool:
        return self.score is not None

    def size_multiplier(self, cfg) -> float:
        """Linear taper from 1.0 (calm) to the configured floor (stressed).

        Unknown regime multiplies by 1.0: absence of evidence about the
        environment is not evidence the environment is hostile.
        """
        if self.score is None:
            return 1.0
        floor = cfg.regime.min_size_multiplier
        return 1.0 - (1.0 - floor) * self.score

    def vrp_shift(self, cfg) -> float:
        """How far stress moves the VRP bounds toward selling premium.

        Centred at zero when the composite sits at 0.5: a normal market shifts
        nothing. Positive in stress (premium genuinely richer -> selling is
        better paid, buying is more expensive), negative in unusual calm.
        """
        if self.score is None:
            return 0.0
        return cfg.regime.max_vrp_shift * (self.score - 0.5) * 2

    def as_dict(self) -> dict:
        return {"score": self.score, "factors": self.factors, "detail": self.detail}


def percentile_of_last(series: list[float]) -> float | None:
    """Where the latest value sits within the series, in [0, 1]."""
    if len(series) < 10:
        return None
    last = series[-1]
    below = sum(1 for v in series[:-1] if v <= last)
    return below / (len(series) - 1)


def ratio_series(a: list[float], b: list[float]) -> list[float]:
    n = min(len(a), len(b))
    return [x / y for x, y in zip(a[-n:], b[-n:], strict=True) if y > 0]


def compute(market_data, universe: set[str], cfg) -> RegimeReading:
    """Build the composite from daily closes. Cached upstream by MarketData."""
    lookback = cfg.regime.lookback_days
    factors: dict[str, float] = {}

    for name, (numerator, denominator) in FACTOR_PAIRS.items():
        try:
            num = market_data.daily_closes(numerator, lookback)
            den = market_data.daily_closes(denominator, lookback)
        except Exception:  # noqa: BLE001, S112 -- a missing proxy drops its
            # factor from the composite; the reading reports what it had.
            continue
        pct = percentile_of_last(ratio_series(num, den))
        if pct is not None:
            factors[name] = round(pct, 3)

    # Breadth: fraction of the universe trading above its own 20-day mean.
    above = total = 0
    for symbol in sorted(universe)[:20]:  # a sample is plenty for a composite
        try:
            closes = market_data.daily_closes(symbol, 30)
        except Exception:  # noqa: BLE001, S112 -- one unavailable symbol
            # shrinks the breadth sample rather than aborting it.
            continue
        if len(closes) >= 20:
            total += 1
            if closes[-1] > sum(closes[-20:]) / 20:
                above += 1
    if total >= 10:
        factors["breadth"] = round(1.0 - above / total, 3)  # inverted: weak = stressed

    if not factors:
        return RegimeReading(None, {}, "regime unknown — no factors available")

    score = round(sum(factors.values()) / len(factors), 3)
    label = "calm" if score < 0.35 else "stressed" if score > 0.65 else "normal"
    parts = ", ".join(f"{k}={v:.2f}" for k, v in sorted(factors.items()))
    return RegimeReading(score, factors, f"{label} ({score:.2f}): {parts}")
