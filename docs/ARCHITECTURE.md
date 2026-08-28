# Architecture — the 11 layers

Most retail systems have three of these. Professional grade is having all eleven.

| # | Layer | Spec |
|---|---|---|
| 1 | **Context/data** | Alpaca market data + News API (history to 2015). Universe: ~40–60 liquid optionable names, recomputed daily. Every feature stamped `observed_at`; automated look-ahead test. |
| 2 | **Signal** | Filter → Fireworks analyst (JSON only) → edge test (expected vs IV-implied move). LLM estimates magnitude; code decides structure. |
| 3 | **Sizing** | R = 0.5% × meta-label multiplier (p<0.55 → none; 0.55–0.85 → 0.4–1.0× linear) ∧ vol-target overlay; take the smaller. Caps 1.5%/position, 3%/underlying. |
| 4 | **Risk gate** | Pure function, 13 ordered checks: kill switch, market window, reconciliation clean, defined-risk assert per leg, max loss cap, heat cap, Greeks bands, concentration, correlation, liquidity, daily loss, rate caps, duplicates. Every result logged, passes included. |
| 5 | **Execution** | Deterministic `client_order_id` (idempotent retries), atomic `mleg` orders, limit-at-mid repricing ladder (never market), circuit breaker 5-fail/60s/half-open. |
| 6 | **Trade management** | Triple barrier: 50% max-profit take (credit) / 2× stop / time exit; break-even at 60% of target; ATR trail on directional; never hold within 24h of expiry; flatten all Thu Sep 3 close. Alpaca has no bracket/OTO on mleg — this layer IS our bracket. |
| 7 | **Portfolio risk** | Σ max-loss ≤ 6% equity; net delta-dollars ±$15k; vega/gamma caps; ≤2 positions/underlying; sector + correlation caps. |
| 8 | **Account guards** | Separate supervisor process with its own client and connection pool: −2% daily halt, −6% max-DD halt (manual reset), loss-streak size halving, kill switch (file + CLI), 90s heartbeat flatten. |
| 9 | **Reconciliation** | Local vs broker vs sum-of-fills every cycle. Mismatch → HALT until explained. State rebuilds from broker + audit log on boot. |
| 10 | **Audit log** | Append-only hash-chained JSONL, one record per decision (inputs, LLM rationale + prompt hash, edge numbers, gate results, fills, exits, P&L). Doubles as ML training set and demo material. |
| 11 | **Validation** | No backtester (deliberate — LLM look-ahead bias lives in weights). Instead: replay harness for warm-start only, hypothesis property tests on the gate, chaos tests on execution, automated look-ahead audit. |

## Bandit arms (all defined-risk by construction)
bull put spread · bear call spread · iron condor · call debit spread · put debit spread · long strangle

## Process topology
```
droplet ── docker compose
  ├── trader      (pipeline layers 1–7, 9–10)
  ├── supervisor  (layer 8 — own Alpaca session, polls independently)
  └── dashboard   (FastAPI + SSE + static React build, read-only,
  │                reasoning layer only — no broker-view duplication)
      └── Caddy (TLS) → public demo URL
```
