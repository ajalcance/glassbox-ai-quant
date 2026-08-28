"""The LLM analyst.

Its only job is reading unstructured text and returning structured estimates.
It never names a strategy, never sizes anything, and never decides to trade.
It answers one question: how far do you think this name moves, in what
direction, and over what horizon?

Everything downstream is deterministic. That separation is what makes an LLM
defensible in a trading path at all — the research is unambiguous that language
models are unreliable alpha sources but genuinely good text extractors.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

SYSTEM_PROMPT = """\
You are a sell-side equity analyst reading a breaking news item. Estimate how \
far the underlying stock will move as a result of THIS news, and how confident \
you are.

Rules:
- Estimate the magnitude of the move, not whether to trade. You are not \
choosing a strategy, a structure, or a size.
- expected_move_pct is the absolute percentage move you expect over your stated \
horizon, as a positive number. A typical single-stock reaction to routine news \
is 0.5-2%; a genuine surprise is 3-8%; only exceptional news exceeds 10%.
- If the news is stale, already widely known, or immaterial to the share price, \
set materiality low and expected_move_pct near zero. Saying "this does not \
matter" is a useful answer.
- direction is "up", "down", or "vol_only" when a large move is likely but its \
sign is genuinely unclear.
- Do not speculate beyond what the text supports. Do not invent numbers.

Return only JSON matching the schema."""


class AnalystView(BaseModel):
    """Structured estimate. Deliberately contains no trade instruction."""

    event_type: str = Field(description="short label, e.g. earnings, guidance, M&A, legal")
    direction: str = Field(description="up | down | vol_only")
    confidence: float = Field(ge=0.0, le=1.0)
    expected_move_pct: float = Field(ge=0.0, le=100.0)
    horizon_hours: float = Field(gt=0.0, le=336.0)
    materiality: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=400)

    @property
    def is_directional(self) -> bool:
        return self.direction in ("up", "down")


def build_user_prompt(headline: str, summary: str, symbol: str, source: str) -> str:
    return f"Symbol: {symbol}\nSource: {source}\nHeadline: {headline}\nSummary: {summary[:1500]}"


def analyse(llm, cfg, *, symbol: str, headline: str, summary: str, source: str) -> AnalystView:
    """Raises LlmUnavailableError / LlmSchemaError; the caller drops the event."""
    return llm.extract(
        model=cfg.llm.analyst_model,
        system=SYSTEM_PROMPT,
        user=build_user_prompt(headline, summary, symbol, source),
        schema=AnalystView,
        max_tokens=cfg.llm.analyst_max_tokens,
    )
