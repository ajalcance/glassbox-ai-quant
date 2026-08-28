# GlassBox AI Quant

**A news-driven, risk-gated options trading agent whose every decision is inspectable — the opposite of a black box.**

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon) (lablab.ai × Alpaca, Aug 28 – Sep 4 2026). Runs entirely against Alpaca's **paper trading** environment.

## What it does

News arrives on Alpaca's real-time stream. A deterministic filter kills ~95% of it. Survivors go to an LLM (Fireworks) that does the one job LLMs are best at — reading unstructured text — and returns **structured estimates, never trade instructions**. Deterministic code then asks the only question that matters in options:

> **Is the move I expect bigger or smaller than the move the options are already pricing?**

That comparison — expected move vs. IV-implied move — is the signal. A meta-labeler (LightGBM, trained on the agent's own past trades) scores how much to trust it. A contextual bandit (Thompson sampling) picks which defined-risk options structure fits the regime. A non-bypassable 13-check risk gate approves or vetoes. Every step is written to a hash-chained audit log.

**The governing invariant:** *ML sets selection and sizing. Deterministic code sets limits.* A model failure degrades to "sized too small" — never to "lost the account."

## Architecture

```
Alpaca news stream (WebSocket)
  └─ deterministic filter (universe · novelty · materiality · liquidity)
      └─ LLM analyst → {direction, confidence, expected_move_pct, horizon}  (JSON only)
          └─ EDGE TEST: expected move vs IV-implied move (ATM straddle)
              └─ meta-labeler P(profit) → position size
                  └─ bandit selects defined-risk structure (6 arms)
                      └─ RISK GATE (13 checks, pure function, can veto)
                          └─ idempotent multi-leg execution (client_order_id, circuit breaker)
                              └─ triple-barrier trade manager (target / stop / time)
                                  └─ hash-chained audit log ──→ retrains meta-labeler + bandit
```

An independent **supervisor process** (own process, own client and connection pool) enforces daily loss limits, max drawdown, and the kill switch — a deadlocked trader cannot block the emergency path.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full 11-layer design and [docs/PLAN.md](docs/PLAN.md) for the hackathon plan.

## Stack

- **Python 3.12** · alpaca-py · scikit-learn · FastAPI (SSE) — backend
- **Fireworks AI** — LLM analyst (structured JSON extraction) + nightly report writer
- **FastAPI + SSE, single self-contained HTML page** — read-only dashboard (the reasoning layer, not a second broker UI)
- **SQLite (WAL) + append-only JSONL audit log** — storage
- **Docker Compose on a $12 DigitalOcean droplet** — deployment

## Quickstart

```bash
cp .env.example .env          # fill in Alpaca paper + Fireworks keys
pip install uv && uv sync

uv run python -m glassbox.smoke              # verify connectivity
uv run python -m glassbox.ml.train           # fit the frozen volatility model
uv run python -m glassbox.runner --dry-run   # full pipeline, live data, no orders
uv run python -m glassbox.supervisor.run     # guards + kill switch (separate process)
uv run python -m glassbox.dashboard.app      # dashboard on :8847
```

Requires an Alpaca **paper** account (free, no funding) and a Fireworks API key.

## The dashboard shows what Alpaca can't

Alpaca's own dashboard already shows equity, positions, P&L and orders, so we
don't rebuild any of it. GlassBox surfaces only what lives in the audit log:
the news event and reasoning behind each decision, the **trades that were vetoed
and never submitted** (which the broker has no record of), risk-budget
utilization against our own caps, and the state of the learning components.

*Alpaca shows what happened. GlassBox shows why — and what we deliberately didn't do.*

## What this deliberately does NOT include (and why)

- **No backtester** — the LLM's training data overlaps any historical window, so a backtest would inherit look-ahead bias baked into model weights. We warm-start from historical news replay and label it exactly that.
- **No deep RL** — a 4-session contest produces tens of trades; PPO/DQN need ~10⁵ episodes. Contextual bandits converge at this sample size, so that's what we use.
- **No martingale, grid, or averaging down. No naked options. No parameter optimization** — every constant is hand-chosen and justified in config.

## Disclaimers

This is a hackathon research project operating exclusively on **simulated (paper) trading**. Nothing in this repository is investment advice or a recommendation to buy or sell any security. Paper-trading results are hypothetical and do not represent actual trading. Options trading involves substantial risk and is not suitable for all investors.

## License

[MIT](LICENSE)
