# GlassBox AI Quant — Hackathon Plan

> **Historical document.** This is the plan as written before the contest, kept
> unedited as a record of intent. Where it and the code disagree, the code and
> the README are current: the gate grew to 19 checks, the triple barrier became
> eleven severity-ordered barriers, and R was raised, reverted and restored.
> Outcomes are in the README.


**One-liner:** a news-driven, risk-gated options desk whose every decision is inspectable — the opposite of a black box.

**Governing invariant:** *ML sets selection and sizing. Deterministic code sets limits.* A model failure degrades to "sized too small," never to "lost the account."

## Locked decisions

| Decision | Choice |
|---|---|
| Entry | Solo · Python · lablab.ai Alpaca AI Trading Agents Hackathon (Aug 28–Sep 4 2026) |
| Repo | github.com/ajalcance/glassbox-ai-quant — public, MIT, constants in `config/*.yaml` |
| LLM | Fireworks (OpenAI-compatible; fast model for analyst, large model for nightly reports) |
| Trigger | Event-driven off Alpaca news WebSocket + 60s polled heartbeat fallback |
| Autonomy | Fully unattended overnight (PHT); independent supervisor owns the kill switch |
| Excluded | Backtester, deep RL, martingale/grid/averaging-down, short-horizon trend, naked options, parameter optimization |

## Signal core

News stream → deterministic filter (kills ~95%) → LLM analyst (forced JSON: direction, confidence, expected_move_pct, horizon, novelty) → **edge test**: expected move vs IV-implied move (ATM straddle) →
- expected ≫ implied → debit spread (long convexity)
- expected ≪ implied → credit spread / iron condor (short premium)
- expected ≈ implied → **no trade**

Then: meta-labeler P(profit) sets size → bandit picks structure → risk gate → execution → triple-barrier management → outcome retrains the ML.

## ML stack

| Component | Method | Why |
|---|---|---|
| Meta-labeler | Regularised logistic regression on triple-barrier outcomes of own trades | Learns from past entries, and abstains below 30 — a boosted ensemble on forty rows is noise fitted with conviction |
| Structure selector | Thompson-sampling bandit, 6 defined-risk arms, warm-started from news-history replay | Correct RL class for tens-of-pulls sample sizes |
| Vol forecaster | HAR-RV, pretrained offline, frozen | Vol is forecastable; returns aren't. Feeds the edge test |

## Position sizing over a position's life

Size is set at entry and never changes: the manager holds whole or closes whole.
Scaling out is not a design choice we declined but one unavailable to us — at
this risk budget a typical spread is one to a few contracts, and partial exits
on a two-leg structure are not worth the extra execution surface. Scaling in is
declined
deliberately, since adding to an open thesis raises risk on a view already in
the market.

Adaptation therefore happens *before* entry rather than during. Each new
position is sized through a multiplicative chain where every factor can shrink
and none can inflate: meta-label confidence, market regime, macro proximity,
portfolio heat, and daily drawdown. The last two turn hard limits into
gradients — previously a book at 5.9% of a 6% heat cap took a full-size position
and one at 6.1% took none, which is a tripwire rather than risk management.

## Risk numbers (per $100k, all in config)

R = 1.0%/trade · max loss 1.5%/position, 3%/underlying · heat ≤ 6% · delta-dollars ±$40k · vega/gamma capped · daily loss −2% → halt · max DD −6% → halt · 4 straight losses → half size · p < 0.55 → no trade · flatten everything **Thu Sep 3 at close**.

## Dashboard (demo URL)
Read-only single-page dashboard served by FastAPI with SSE. Vanilla JS and one
self-contained HTML file — no build step, no bundler, no node_modules on the
droplet. The page reads a JSONL log and renders panels; a second toolchain
would buy nothing and add something else to break on demo day. Controls are
CLI-only.

**Scope rule: we do not rebuild what Alpaca already provides.** Its dashboard
shows equity curve, positions, P&L and the order blotter; ours shows only what
lives in our audit log and cannot exist on the broker's side — the *reasoning*
layer. A permanent "View account on Alpaca" link frames the two as complementary.

Panels: **decision feed** (news -> extraction -> edge test -> per-check gate
PASS/VETO, the centerpiece) - **veto log** (trades never submitted, which the
broker has no record of) - **risk budget** (heat vs our 6% cap, delta-dollars vs
our band, R utilization) - **learning state** (bandit posteriors, meta-labeler
P(profit)) - **system health** (websocket, circuit breaker, reconciliation,
supervisor heartbeat, halt reason, audit-chain verified) - **P&L by bandit arm**
(attribution, not raw equity) - nightly reports.

## Reporting
Nightly cron after US close: P&L, trades, vetoes + reasons, risk utilization, learning deltas; Fireworks large model writes the narrative. One markdown/day → dashboard + social post seed; Thursday's report drafts the required one-pager.

## Market regime and the economic calendar

The news pipeline answers "is this stock mispriced". Two additions supply the
context that question is asked in:

**Regime layer.** A cross-asset risk read from instruments already on Alpaca —
volatility term structure (VXX/VXZ), risk appetite (SPHB/SPLV), credit stress
(HYG/LQD), breadth (SPY/RSP vs the universe) — each scored as a percentile
against its own 60-day history and composited to one score in [0,1]. Regime
never creates a trade: it scales size (multiplier in [0.4, 1.0]) and leans the
VRP bounds toward selling premium when stress makes premium genuinely richer.
Multiplicative, so it can veto toward zero but never inflate past the caps.
Logged on every decision from the first tick, so by end of week there is
evidence for whether it deserves promotion to a first-class signal.

**Macro-event blackout.** The edge test compares a stock-specific expectation
against a straddle that, near a scheduled macro release, carries embedded event
premium — making everything look overpriced and steering the system into
selling insurance right before the event. Contest week is a minefield: ISM +
JOLTS on Sep 1, ADP on Sep 2, NFP on submission morning. Within a window before
each high-impact release (default 2h before to 30min after), no new short
premium; other entries are haircut. The release itself then generates news that
flows through the existing pipeline against post-event straddles — so "trade
the release" emerges from infrastructure we already have, after the dust
settles rather than into it. Contest week uses a hand-verified table in config
(zero dependencies, auditable); the interface takes a calendar API later.

## Additions found by auditing the MCP surface

Reviewing the 72 tools Alpaca's MCP server exposes surfaced five capabilities
the system was not using, two of which were real risk holes rather than missing
features:

1. **Corporate actions** — a short call in a name going ex-dividend carries
   early-assignment risk, and a split changes contract terms mid-position. The
   gate had sixteen checks and none asked whether something was about to happen
   to the security itself. Now a blackout check.
2. **Assignment detection** — an assigned short leg becomes stock, and position
   reconciliation over option symbols would not cleanly catch it. Polled
   separately as `OPASN`/`OPEXP` activity.
3. **Broker portfolio history** — the max-drawdown guard measured from a peak
   held in local state, so wiping the database reset the peak and silently
   disabled the guard until a new one formed. The high-water mark now comes from
   the broker.
4. **Market calendar** — `minutes_to_close` assumed a 16:00 close; an early
   close broke the arithmetic. No half-day falls inside the contest, but it is a
   correctness gap and cheap to close.
5. **Startup preflight** — options level and PDT status are asserted at boot
   instead of discovered mid-session.

Deliberately *not* taken: market movers as a signal feature (an unvalidated
input to a model with no training data yet), and threshold tuning against
historical expired-option trades (parameter fitting on history, which the design
rules out elsewhere).

## MCP and CLI

Both are used, each where it fits. MCP is the human-facing lane — `.mcp.json`
lets any MCP client inspect the account conversationally, and the agent never
routes an order through it. The CLI is an independent verification source for
the nightly cross-check: a different binary and code path from the trader, so a
disagreement means one of the two views of the account is actually wrong.
Measured caveat: CLI v0.0.13 completes roughly one call in six, which is exactly
why the cross-check is best-effort, report-time, and reports *unavailable*
rather than silent agreement.

## Infrastructure
DigitalOcean droplet 2GB/1vCPU ($12/mo hourly ≈ $3/week), NYC, Ubuntu 24.04. Docker Compose: `trader` · `supervisor` · `dashboard` + Caddy TLS. UFW 22/80/443. Secrets via `.env` on host only. Models train locally; frozen artifacts ship. Total run cost ≲ $15.

## The best-idea floor

A pure threshold system can end a five-day leaderboard window silent, which
makes the P&L criterion unjudgeable. The floor is the honest fix: **each day,
if nothing has traded organically by mid-afternoon, express the single best
idea the pipeline saw at reduced size** — the signal that reached the edge test
and was refused *only* for sitting inside the ratio band. Signals refused for
confidence, bad data, VRP, or any gate check are never floor candidates; the
floor relaxes exactly one bar (the ratio band), once per day, at half risk, and
the full gate still applies at execution time. Every floor trade is
labelled as such in the audit log — funds run "best ideas" books, and this is
one, not a pretended threshold crossing.

The cadence is the C half of the hybrid: after each close, `make calibrate`
shows the day's ratio distribution, thresholds are adjusted from evidence, and
the floor stays enabled only until organic flow proves sufficient.

## Testing and drills

Everything is unit-tested and the pipeline has run end to end against live data,
but **no order has ever actually filled**. Placing an unfillable order and
cancelling it exercises submission; it does not exercise fills, position state,
barrier management, closing, P&L realisation, labelling, or any of the learning
that depends on a closed position. Those paths are unproven until a real trade
completes.

Drills run on the **development account** (`PA3CYQV2PBDK`). The brief permits
"any paper account you like during development"; the contest account is created
fresh and only ever sees contest trading.

| # | Drill | Proves |
|---|---|---|
| 0 | **Simulated lifecycle** | Everything downstream of a fill — position state, reconciliation, heat, barriers, close, P&L, labelling, both learners, audit chain — against real quotes with only the fill invented. Runs with the market closed |
| 1 | **Round trip** | A marketable spread fills, position is recorded, reconciliation stays clean, the manager evaluates it, the close fills, P&L is realised, the outcome is labelled, the bandit posterior updates and a training row appears |
| 2 | **Supervisor flatten** | With a real position open, the kill switch causes the supervisor to actually close it |
| 3 | **Reconciliation divergence** | An induced mismatch between local state and the broker halts trading |
| 4 | **Deadline flatten** | A near-term deadline closes everything, including a winning position |
| 5 | **Unattended session** | The full stack survives a live session with nobody watching |

Objective is mechanical correctness, not P&L. A drill that loses a few dollars
and proves the close path works is a success.

### Account timing

The contest account must exist **before live trading**, not at submission. The
leaderboard window is only five days, so trading starts Monday — which means the
fresh account and its keys are created **over the weekend**, drills run on the
dev account only, and Monday morning runs preflight against the contest account
before the open.

## Build schedule

| Day | Ship |
|---|---|
| Fri Aug 28 | Scaffold, config schema, fresh paper account @ $100k, auth + data smoke test, audit skeleton · post #1 |
| Sat Aug 29 | Data / execution / reconciliation; place + cancel real multi-leg paper order · post #2 |
| Sun Aug 30 | Sizing, gate, portfolio risk, supervisor; property + chaos tests; droplet deployed · post #3 |
| Mon Aug 31 | News pipeline + analyst + edge test; trade manager; dashboard skeleton; **dry-run live session** |
| Tue Sep 1 | Replay warm-start, train meta-labeler; **go live**; dashboard live feed · post #4 |
| Wed Sep 2 | Reporting; MCP + CLI polish; UI polish |
| Thu Sep 3 | Final session; **flatten at close**; video, slides, one-pager · post #5 |
| Fri Sep 4 | Submit before 15:00 UTC |

## Submission checklist
Public repo · demo URL · video · slides · cover image · **new** paper account ID ($100k start) · one-pager (AI logic / risk gates / Alpaca infra) · ≤5 social links (tag @lablabai + @AlpacaHQ).

## Scoring map
P&L → defined-risk + guards keep the account alive. Tech → full Alpaca surface (Trading API, News API, market data, mleg options, MCP, CLI). Creativity → edge test, hash-chained log, meta-labeling, bandits, independent supervisor. Presentation → dashboard demo, gate-veto on camera, one-pager mirrors required structure. Social → 5 posts fed by nightly reports.
