"""Verify the Fireworks connection and resolve real model ids.

    uv run python -m glassbox.verify_llm

Model catalogues change, so the configured ids are checked against what this
account can actually serve rather than trusted. Then a real news item is run
through the analyst and the edge test end to end.
"""

from __future__ import annotations

import sys

from glassbox.config import load_config
from glassbox.llm import LlmClient, LlmSchemaError, LlmUnavailableError
from glassbox.signal.analyst import analyse
from glassbox.signal.edge import evaluate_edge

SAMPLE = {
    "symbol": "AAPL",
    "headline": "Apple beats Q3 estimates, raises full-year guidance on iPhone strength",
    "summary": (
        "Apple reported Q3 EPS of $1.72 versus $1.61 expected and revenue of $92.4B "
        "versus $89.1B expected. The company raised full-year revenue guidance, citing "
        "stronger than anticipated iPhone demand in emerging markets."
    ),
    "source": "benzinga",
}


def main() -> int:
    cfg = load_config()
    try:
        llm = LlmClient.from_config(cfg)
    except RuntimeError as e:
        print(f"FAIL  {e}")
        return 1

    print("Resolving models available to this account...")
    try:
        models = llm.list_models()
    except Exception as e:  # noqa: BLE001 -- surface any connection problem plainly
        print(f"FAIL  could not list models: {type(e).__name__}: {e}")
        return 1

    print(f"  {len(models)} model(s) available")
    for m in models[:15]:
        print(f"    {m}")
    if len(models) > 15:
        print(f"    ... and {len(models) - 15} more")

    configured = {"analyst": cfg.llm.analyst_model, "report": cfg.llm.report_model}
    missing = {role: mid for role, mid in configured.items() if mid not in models}
    for role, mid in configured.items():
        mark = "ok   " if mid in models else "MISS "
        print(f"  {mark} {role}: {mid}")
    if missing:
        print("\nConfigured model id(s) not in this account's catalogue.")
        print("Pick from the list above and update config/default.yaml -> llm.")
        return 1

    print("\nRunning the analyst on a sample story...")
    try:
        view = analyse(llm, cfg, **SAMPLE)
    except (LlmUnavailableError, LlmSchemaError) as e:
        print(f"FAIL  analyst: {type(e).__name__}: {e}")
        return 1

    print(f"  event_type        {view.event_type}")
    print(f"  direction         {view.direction}")
    print(f"  confidence        {view.confidence:.2f}")
    print(f"  expected_move_pct {view.expected_move_pct:.2f}%")
    print(f"  horizon_hours     {view.horizon_hours:.0f}")
    print(f"  materiality       {view.materiality:.2f}")
    print(f"  rationale         {view.rationale[:120]}")

    # A $4 straddle on a $230 stock implies roughly a 1.7% move to expiry.
    edge = evaluate_edge(
        expected_move_pct=view.expected_move_pct,
        direction=view.direction,
        confidence=view.confidence,
        straddle_mid=4.00,
        spot=230.00,
        hours_to_expiry=168.0,
        horizon_hours=view.horizon_hours,
        cfg=cfg,
    )
    print("\nEdge test against a sample chain:")
    print(f"  verdict    {edge.verdict}")
    print(f"  detail     {edge.detail}")
    print(f"  structures {[str(s) for s in edge.eligible_structures] or 'none'}")

    print("\nLLM VERIFY PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
