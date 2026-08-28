# Backend conventions (AI development guide)

Read this before writing or modifying any Python in `glassbox/`.

## Structure
```
glassbox/
  data/        # market data + news clients, point-in-time DecisionContext
  signal/      # filter → LLM analyst → edge test
  ml/          # meta-labeler, bandit, vol forecaster (inference only in prod)
  gate.py      # THE risk gate — pure function, 13 ordered checks
  sizing.py    # R-based sizing ∧ vol target, take the min
  execution/   # order builder, mleg submission, repricing ladder, circuit breaker
  manage/      # triple-barrier trade manager, break-even, ATR trail
  portfolio/   # heat, Greeks bands, concentration, correlation
  reconcile.py # 3-way position reconciliation, HALT on mismatch
  audit.py     # hash-chained JSONL writer
  supervisor/  # SEPARATE entrypoint, own credentials — never import trader state
  report/      # nightly report generator
```

## Rules
- **Pure functions for decisions.** Gate, sizing, edge test take frozen dataclasses in, verdict out. No I/O inside decision code — makes them property-testable.
- Every external call: timeout + exponential backoff with jitter. Wrap Alpaca calls in the circuit breaker (5 fails → open 60s → half-open).
- Every feature carries `observed_at`; never use a value whose `observed_at > decision_time` (test enforces).
- LLM calls: JSON schema forced, response validated with pydantic, one retry on validation failure, then discard the event (log the discard).
- Config via pydantic-settings loading `config/*.yaml` + `.env`. No constant appears twice.
- Errors in the trading loop never crash the process: catch, log to audit, continue or HALT — chosen explicitly, never implicitly.
- Log the *decision not to trade* with the same fidelity as trades. Vetoes are data.
- Tests: property tests for the gate (hypothesis), chaos tests for execution (injected 500s/timeouts), fixture-replay tests for the signal pipeline.
