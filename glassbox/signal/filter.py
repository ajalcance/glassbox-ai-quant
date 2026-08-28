"""Deterministic pre-filter.

Runs before the LLM and rejects roughly 95% of the news stream at effectively
zero cost. Every rejection is recorded, so the veto log shows what we ignored
and why, not just what we traded.

Novelty uses token overlap rather than embeddings — a repeated story is
lexically similar, and a similarity model would add a dependency and a failure
mode for no gain at this scale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# Headlines that are structurally noise regardless of symbol.
BOILERPLATE = (
    "market update",
    "stocks moving",
    "movers",
    "watchlist",
    "what to watch",
    "premarket",
    "market recap",
    "closing bell",
    "opening bell",
    "top gainers",
    "top losers",
    "unusual options activity",
    "here's how much",
    "if you invested",
)

_TOKEN = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "with",
        "as",
        "at",
        "by",
        "from",
        "this",
        "that",
        "it",
        "its",
        "has",
        "have",
        "had",
        "will",
        "would",
        "could",
        "after",
        "before",
        "said",
        "says",
    ]
)


def tokenise(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True, slots=True)
class NewsItem:
    id: str
    symbol: str
    headline: str
    summary: str
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FilterResult:
    passed: bool
    reason: str
    novelty: float = 1.0

    def __str__(self) -> str:
        return f"{'PASS' if self.passed else 'DROP'}: {self.reason}"


class NewsFilter:
    """Stateful only in that it remembers recent headlines per symbol."""

    def __init__(self, universe: set[str], cfg, similarity_threshold: float = 0.6):
        self.universe = universe
        self.window = timedelta(hours=cfg.novelty_window_hours)
        self.threshold = similarity_threshold
        self._seen: dict[str, list[tuple[datetime, frozenset[str]]]] = {}

    def _prune(self, symbol: str, now: datetime) -> list[tuple[datetime, frozenset[str]]]:
        recent = [(t, toks) for t, toks in self._seen.get(symbol, []) if now - t <= self.window]
        self._seen[symbol] = recent
        return recent

    def novelty(self, item: NewsItem) -> float:
        """1.0 = never seen anything like it; 0.0 = a verbatim repeat."""
        recent = self._prune(item.symbol, item.created_at)
        if not recent:
            return 1.0
        tokens = tokenise(f"{item.headline} {item.summary}")
        return 1.0 - max(jaccard(tokens, prev) for _, prev in recent)

    def remember(self, item: NewsItem) -> None:
        self._seen.setdefault(item.symbol, []).append(
            (item.created_at, tokenise(f"{item.headline} {item.summary}"))
        )

    def evaluate(self, item: NewsItem, now: datetime | None = None) -> FilterResult:
        now = now or item.created_at

        if item.symbol not in self.universe:
            return FilterResult(False, f"{item.symbol} not in tradable universe")

        headline_lower = item.headline.lower()
        for phrase in BOILERPLATE:
            if phrase in headline_lower:
                return FilterResult(False, f"boilerplate headline ({phrase!r})")

        if len(item.headline) < 15:
            return FilterResult(False, "headline too short to carry information")

        age = now - item.created_at
        if age > timedelta(hours=2):
            return FilterResult(False, f"stale by {age.total_seconds() / 3600:.1f}h")

        score = self.novelty(item)
        if score < (1.0 - self.threshold):
            return FilterResult(False, f"novelty {score:.2f} — restatement of recent news", score)

        self.remember(item)
        return FilterResult(True, f"novel ({score:.2f}), fresh, in universe", score)
