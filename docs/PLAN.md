# GlassBox AI Quant — Hackathon Plan

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
| Meta-labeler | LightGBM on triple-barrier outcomes of own trades | Learns from past entries; converges on hundreds of rows |
| Structure selector | Thompson-sampling bandit, 6 defined-risk arms, warm-started from news-history replay | Correct RL class for tens-of-pulls sample sizes |
| Vol forecaster | HAR-RV, pretrained offline, frozen | Vol is forecastable; returns aren't. Feeds the edge test |

## Risk numbers (per $100k, all in config)

R = 0.5%/trade · max loss 1.5%/position, 3%/underlying · heat ≤ 6% · delta-dollars ±$15k · vega/gamma capped · daily loss −2% → halt · max DD −6% → halt · 4 straight losses → half size · p < 0.55 → no trade · flatten everything **Thu Sep 3 at close**.

## Dashboard (demo URL)
Read-only React SPA (Vite + Tailwind) over FastAPI + SSE. Controls are CLI-only.

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

## Infrastructure
DigitalOcean droplet 2GB/1vCPU ($12/mo hourly ≈ $3/week), NYC, Ubuntu 24.04. Docker Compose: `trader` · `supervisor` · `dashboard` + Caddy TLS. UFW 22/80/443. Secrets via `.env` on host only. Models train locally; frozen artifacts ship. Total run cost ≲ $15.

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
