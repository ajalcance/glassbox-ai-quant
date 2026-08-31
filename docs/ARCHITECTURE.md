# Architecture — the 11 layers

Most retail systems have three of these. Professional grade is having all eleven.

| # | Layer | Spec |
|---|---|---|
| 1 | **Context/data** | Alpaca market data + News API (history to 2015). Universe: ~40–60 liquid optionable names, recomputed daily. Every feature stamped `observed_at`; automated look-ahead test. **Market calendar** supplies real session times, so an early close is not assumed to be 16:00. |
| 2 | **Signal** | Filter → Fireworks analyst (JSON only) → edge test (expected vs IV-implied move). LLM estimates magnitude; code decides structure. |
| 3 | **Sizing** | R = 0.5% × meta-label multiplier (p<0.55 → none; 0.55–0.85 → 0.4–1.0× linear) ∧ vol-target overlay; take the smaller. Caps 1.5%/position, 3%/underlying. |
| 4 | **Risk gate** | Pure function, 18 ordered checks: kill switch, market window, reconciliation clean, defined-risk assert per leg, max loss cap, heat cap, Greeks bands, concentration, correlation, liquidity, daily loss, rate caps, duplicates, **session room** — an intraday thesis needs half its horizon before the close or it cannot resolve — a **macro blackout** — no new short premium into a scheduled release, whose event premium is exactly what makes the edge test misread — and a **corporate-action blackout** — no new position in a name with a dividend, split, merger or spinoff inside the horizon. Every result logged, passes included. |
| 5 | **Execution** | **Order lifecycle**: every in-flight order is polled each tick — an entry fill flips the position to managed and recomputes risk from the actual price; a dead entry frees its heat; a stale entry is cancelled, not chased; an unfilled close escalates at progressively worse prices (bounded, with the supervisor flatten as the backstop beyond it). Deterministic `client_order_id` (idempotent retries), atomic `mleg` orders, limit-at-mid repricing ladder (never market), circuit breaker 5-fail/60s/half-open. |
| 6 | **Trade management** | **Size never changes once opened** — binary hold or close. Scaling out is unavailable at one-contract positions; scaling in is declined as adding risk to an open thesis. Risk is reduced through exits instead. An intraday horizon is truncated to the closing bell, so a thesis that expired at the close is never carried overnight unmanaged. Triple barrier: 50% max-profit take (credit) / 2× stop / time exit; break-even at 60% of target; ATR trail on directional; never hold within 24h of expiry; flatten all Thu Sep 3 close. Alpaca has no bracket/OTO on mleg — this layer IS our bracket. |
| 7 | **Portfolio risk** | Σ max-loss ≤ 6% equity; net delta-dollars ±$40k; vega/gamma caps; ≤2 positions/underlying; sector + correlation caps. |
| 8 | **Account guards** | Separate supervisor process with its own client and connection pool: −2% daily halt, −6% max-DD halt (manual reset), loss-streak size halving, kill switch (file + CLI), 90s heartbeat flatten. |
| 9 | **Reconciliation** | Local vs broker vs sum-of-fills every cycle. Mismatch → HALT until explained. State rebuilds from broker + audit log on boot. **Option assignment and expiration events** (`OPASN`/`OPEXP`) are polled separately, because an assigned short leg becomes stock and would otherwise leave position state quietly wrong. |
| 10 | **Audit log** | Append-only hash-chained JSONL, one record per decision (inputs, LLM rationale + prompt hash, edge numbers, gate results, fills, exits, P&L). Doubles as ML training set and demo material. |
| 10b | **Analyst scoring** | Every estimate is recorded when made — traded or not — and scored once its horizon elapses. Every entry threshold is a ratio involving the expected move, so all of them rest on whether the analyst's numbers mean what they appear to |
| 11 | **Validation** | No backtester (deliberate — LLM look-ahead bias lives in weights). Instead: replay harness for warm-start only, hypothesis property tests on the gate, chaos tests on execution, automated look-ahead audit. |

## The daily best-idea floor

A pure threshold system can end a five-day leaderboard window silent. If nothing
has traded organically by 14:00 ET, the day's best signal refused *only* for
sitting inside the ratio band is re-entered with the band relaxed and size
halved — reusing the morning's analyst view (a second call could quietly change
the opinion that made it the best idea) and skipping the staleness filter it
already passed. Everything else applies in full: VRP, sizing caps, all 18 gate
checks. One per day; it stands down entirely on any day organic flow trades; a
refused attempt does not consume the day's shot. Every floor record is labelled
in the audit log — this is a best-ideas book, not a pretended threshold
crossing.

## Bandit arms (all defined-risk by construction)
bull put spread · bear call spread · iron condor · call debit spread · put debit spread · long strangle

## Startup preflight

Before the first tick the trader asserts what it depends on rather than
discovering it mid-session: the account is active and unblocked, options are
enabled at the level multi-leg spreads require, PDT status is known, and the
market calendar is reachable. A precondition that fails at 03:00 with positions
open is a much worse way to learn it was never true.

## Process topology
```
droplet ── docker compose
  ├── trader      (pipeline layers 1–7, 9–10)
  ├── supervisor  (layer 8 — own Alpaca session, polls independently)
  └── dashboard   (FastAPI + SSE + static React build, read-only,
  │                reasoning layer only — no broker-view duplication)
      └── Caddy (TLS) → public demo URL
```
