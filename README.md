# GlassBox AI Quant

**A news-driven, risk-gated options trading agent whose every decision is inspectable — the opposite of a black box.**

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon) (lablab.ai × Alpaca, Aug 28 – Sep 4 2026). Runs entirely against Alpaca's **paper trading** environment.

## What it does

News arrives on Alpaca's real-time stream. A deterministic filter kills ~95% of it. Survivors go to an LLM (Fireworks) that does the one job LLMs are best at — reading unstructured text — and returns **structured estimates, never trade instructions**. Deterministic code then asks the only question that matters in options:

> **Is the move I expect bigger or smaller than the move the options are already pricing?**

That comparison — expected move vs. IV-implied move — is the signal. A meta-labeler (regularised logistic regression, trained on the agent's own past trades) scores how much to trust it. A contextual bandit (Thompson sampling) picks which defined-risk options structure fits the regime. A non-bypassable 18-check risk gate approves or vetoes. Every step is written to a hash-chained audit log.

**The governing invariant:** *ML sets selection and sizing. Deterministic code sets limits.* A model failure degrades to "sized too small" — never to "lost the account."

## Architecture

```
Alpaca news stream (WebSocket)
  └─ deterministic filter (universe · novelty · materiality · liquidity)
      └─ LLM analyst → {direction, confidence, expected_move_pct, horizon}  (JSON only)
          └─ EDGE TEST: expected move vs IV-implied move (ATM straddle)
              └─ meta-labeler P(profit) → position size
                  └─ bandit selects defined-risk structure (6 arms)
                      └─ RISK GATE (18 checks, pure function, can veto)
                          └─ idempotent multi-leg execution (client_order_id, circuit breaker)
                              └─ triple-barrier trade manager (target / stop / time)
                                  └─ hash-chained audit log ──→ retrains meta-labeler + bandit
```

A **startup preflight** asserts what the trader depends on — account active,
options enabled at Level 3, trading not suspended, calendar reachable — rather
than discovering any of it mid-session.

An independent **supervisor process** (own process, own client and connection pool) enforces daily loss limits, max drawdown, and the kill switch — a deadlocked trader cannot block the emergency path.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full 11-layer design and [docs/PLAN.md](docs/PLAN.md) for the hackathon plan.

## Stack

- **Python 3.12** · alpaca-py · scikit-learn · FastAPI (SSE) — backend
- **Fireworks AI** — LLM analyst (structured JSON extraction) + nightly report writer
- **FastAPI + SSE, single self-contained HTML page** — read-only dashboard (the reasoning layer, not a second broker UI)
- **SQLite (WAL) + append-only JSONL audit log** — storage
- **Docker Compose on a 2 GB US-East VPS** — deployment (trader, supervisor, scheduler, dashboard, Caddy)

## Quickstart

```bash
cp .env.example .env          # fill in Alpaca paper + Fireworks keys
pip install uv && uv sync

uv run python -m glassbox.smoke              # verify connectivity
uv run python -m glassbox.ml.train           # fit the frozen volatility model
uv run python -m glassbox.runner --dry-run   # full pipeline, live data, no orders
uv run python -m glassbox.supervisor.run     # guards + kill switch (separate process)
uv run python -m glassbox.dashboard.app      # dashboard on :8847
uv run python -m glassbox.report.run         # nightly report + CLI cross-check
uv run python -m glassbox.scheduler          # nightly report + model refit on schedule
uv run python -m glassbox.verify_mcp         # verify the MCP integration
```

Requires an Alpaca **paper** account (free, no funding) and a Fireworks API key.

## Alpaca surface used

- **Trading API** (alpaca-py) — the trading path: chains, quotes, Greeks, atomic multi-leg orders
- **News API** — the WebSocket stream the whole signal pipeline is driven by
- **MCP server** (primary) — `.mcp.json` connects any MCP client so a person can inspect the account in plain language. Verified with `python -m glassbox.verify_mcp`: Alpaca MCP Server 3.4.7, 72 tools. The server *can* trade; the agent deliberately never routes an order through it, because the deterministic gate is the only path to the broker
- **CLI** — an additional independent cross-check in the nightly report. Different binary, different code path, its own auth, so a disagreement means one of the two views of the account is genuinely wrong

See [docs/MCP_AND_CLI.md](docs/MCP_AND_CLI.md), including a measured caveat about CLI reliability.

### Why the order path uses the official SDK

The hackathon guidelines ask that anyone implementing their bot through an SDK
explain the reasons and prioritise the official SDKs. GlassBox uses
**alpaca-py — Alpaca's official Python SDK — for the order path**, with the MCP
server and CLI integrated alongside it. The reason is the governing invariant:

> **The deterministic risk gate must be the only path to the broker.**

An agent that can call a `place_option_order` tool is one hallucinated tool call
away from an unbounded position. That is precisely the failure mode this
architecture exists to prevent, so the LLM is never given a tool that reaches
the broker — it returns schema-validated JSON estimates and nothing else. Four
specific guarantees also require programmatic control of the request itself,
which a conversational tool call cannot provide:

1. **Idempotency** — every order carries a deterministic `client_order_id`
   derived from (signal, structure, attempt), so a retry after a timeout cannot
   double-fill. This is verified under 12-thread concurrent duplicate storms.
2. **Write-ahead intent** — the order is persisted *before* submission, so a
   crash mid-call still leaves something to reconcile against.
3. **Atomicity** — spreads go as single MLEG orders; we are never left holding
   one leg of a defined-risk structure.
4. **Circuit breaking** — broker failures open a breaker rather than becoming a
   retry storm.

The MCP server and CLI are genuinely used, just not as the order path:

- **MCP** is the human inspection surface — 72 tools, verified by a real stdio
  handshake in `verify_mcp`, so a judge or operator can interrogate the same
  account the agent trades, in plain language. The server *can* place orders;
  we deliberately decline to use that capability.
- **CLI** is an independent second source of truth in nightly reconciliation —
  a different binary with its own auth, sharing no code with the SDK path it
  checks. A verification path that shares plumbing with the thing it verifies
  is not verification.

## The dashboard shows what Alpaca can't

Alpaca's own dashboard already shows equity, positions, P&L and orders, so we
don't rebuild any of it. GlassBox surfaces only what lives in the audit log:
the news event and reasoning behind each decision, the **trades that were vetoed
and never submitted** (which the broker has no record of), risk-budget
utilization against our own caps, and the state of the learning components.

*Alpaca shows what happened. GlassBox shows why — and what we deliberately didn't do.*

## Evidence: the guardrails were tested, not asserted

Anyone can claim a kill switch works. These are reproducible drills that fire
the real code against the real broker, plus the results they produced during
the pre-contest weekend. Every one is a `make` target in this repository:

| Drill | What it proves |
|---|---|
| `make drill-sim` | Full lifecycle against a simulated fill — works market-closed |
| `make drill-trip` | Opens a **real** spread, verifies the broker agrees, reconciles, force-closes through an elapsed deadline, feeds the learners, re-verifies the audit chain |
| `make drill-flat` | Opens a position, engages the kill switch, proves the **supervisor** flattens it |
| `make drill-recon` | Induces a state divergence and proves it HALTs trading (places no orders) |
| `make drill-clean` | Sweeps all drill residue |

**These drills earn their keep.** `drill-flat` failed on its first live run
90 minutes before go-live and exposed a real bug in the last-resort safety
path: `close_all_positions` closes a spread's legs as independent positions in
arbitrary order, and selling the long leg while its short leg still existed was
rejected as a would-be naked short — so one pass closed the short leg and left
the long leg on the book, at exactly the moment the guards had decided
everything must go. Flatten now verifies the book is empty, retries, and writes
a loud `flatten_incomplete` audit record if it still cannot finish. Re-run
against the live broker: passed.

Beyond the drills, the agent was soaked under conditions the market cannot be
relied upon to produce:

- **Concurrency** — 12-thread duplicate-news storms through the real pipeline;
  the idempotent `client_order_id` survived every check-then-act race with
  exactly one order reaching the broker each time.
- **Crash recovery** — `SIGKILL` at three points around order submission; state
  rebuilt from the store plus broker, idempotent retry converged on one order.
- **Infrastructure chaos** — every container killed at host level (all revived
  by restart policy), Docker daemon restarted, and a **full host reboot**: the
  entire stack, monitor included, returned unattended with audit chains intact.
- **Network partition** — all outbound HTTPS dropped for three minutes. The
  supervisor kept evaluating guards throughout and recovered cleanly, which is
  only true because every broker call carries an explicit timeout; `alpaca-py`
  sets none, and an unbounded call would silently freeze the one process that
  can least afford to hang.

The audit log is hash-chained per writing process, and `verify_day` re-verified
every chain after each of those scenarios.

## What this deliberately does NOT include (and why)

- **No backtester** — the LLM's training data overlaps any historical window, so a backtest would inherit look-ahead bias baked into model weights. We warm-start from historical news replay and label it exactly that.
- **No deep RL** — a 4-session contest produces tens of trades; PPO/DQN need ~10⁵ episodes. Contextual bandits converge at this sample size, so that's what we use.
- **No martingale, grid, or averaging down. No naked options. No parameter optimization** — every constant is hand-chosen and justified in config.

## Hackathon submission notes

**Pre-event work disclosure.** The hackathon window opened Friday 28 August 2026
at 09:30 ET (13:30 UTC). Work on this repository began roughly eleven hours
earlier, in a single overnight session: the first commit is dated
**28 Aug 2026 02:22 UTC**, and **38 commits spanning 8h49m** landed before the
window opened. Everything after that timestamp was built during the window.
No code predates 28 August, nothing was carried over from an earlier project,
and the full history with timestamps is public in this repository — verify with:

```bash
git log --reverse --format="%aI  %s"
```

**Competition account.** P&L is measured on a dedicated paper account created
for the contest, funded at $100,000, trading from Monday 31 August 09:30 ET.
Development and all drills above ran on a separate testing account, whose
activity forms no part of the official measurement. Positions are flattened
before the close on Thursday 3 September, ahead of the EOD equity snapshot —
`manage.flatten_all_at` in `config/default.yaml` is set to that deadline, which
also avoids any exercise or assignment on 3 September expiries.

**Risk sizing: raised before the window, reverted on day one.** Base risk per
trade is **0.5% of equity**. It was raised to 1.0% on 30 August, then reverted
on 31 August — the first day of the measurement window. Both the change and the
reversal are recorded here rather than quietly settled, because a number that
moved during the contest deserves its reasoning in public.

The raise was mechanical: the meta-labeler abstains until it has 30 labelled
outcomes, which four trading days cannot produce, so its confidence multiplier
was holding every position at roughly 0.2–0.35% of equity — the audit log shows
budgets of $209–$342 against a nominal $500. Raising R restored approximately
the risk the system was designed to take once that multiplier applies.

It also rested on a claim that proved false: *"the delta band caps SPY at a
single spread whatever R says."* That assumed the band would limit position
size. It does not — **the gate is a binary veto, not a size reducer.** At
R=1.0% the sizer asked for 2 SPY spreads carrying −$48,119 of delta against the
±$40,000 band, and the trade was refused outright rather than trimmed to the 1
spread that fits at roughly −$24,000. The 31 August audit log holds both cases:
two qty=1 candidates that cleared the band, and two qty=2 candidates that did
not.

R=1.0% and a $40,000 delta band are jointly inconsistent on the highest-notional
underlying, so one had to give. **Reverting R was the risk-reducing resolution**
for day one. Raising the band was the alternative and was explicitly rejected:
loosening a directional-exposure limit in order to obtain trades is optimisation
on contest data, which this project forbids itself.

**On 1 September the interaction itself was fixed and R restored to 1.0%.** The
pipeline now fits quantity to the delta band before the gate — a candidate that
would breach at qty 2 becomes the qty 1 that fits, exactly as sizing already
tapers for heat and drawdown — instead of the gate answering yes/no on a number
sizing never saw. The same change class covers the opening-auction window,
which used to *discard* signals arriving in its first fifteen minutes
(day one's two strongest, ratios 1.97 and 1.74, were lost this way); they are
now deferred and re-entered once the window opens, with staleness re-enforced
at retry. With the interaction gone, the original pre-window reasoning for
R=1.0% stands unchanged, and the full history — raised, interaction found on
day one, reverted, interaction fixed, restored — is in this section, the config
comments, and the audit log rather than smoothed over.

Every hard limit is unchanged throughout: 1.5% max loss per position, 3% per
underlying, 6% portfolio heat, ±$40k net delta, −2% daily loss halt, −6% max
drawdown halt, and defined risk on every leg. **No risk limit has been widened
at any point.**

**Market data.** Runs on Alpaca's free Basic plan, which provides the
*indicative* options feed rather than OPRA. Pricing uses latest quotes and
snapshots, which are real-time on Basic; the 15-minute restriction applies only
to historical bars and trades, which are used solely for daily realised
volatility.

The liquidity gate's threshold is **calibrated to the feed it measures, from
data rather than guesswork**. `soak/spread_calibration.py` compares the spread
the gate recorded at decision time (indicative) against effective spreads
estimated from real OPRA prints — which Basic *does* provide, delayed —
using intra-minute price range and the Roll (1984) estimator. The 31 August
session showed single-name contracts printing 200–1,475 times at 1.4–3.0%
effective spread while the indicative feed quoted them at 15.0–17.3% — a
5–13× inflation (SPY: only ~1.3–2×). One vetoed AMZN leg had printed 1,475
times that day at ~1.9% real spread. The cap was moved from 10% to 20% *in
indicative units*, which the measurement shows is roughly 4% of real spread
for print-rich names; genuinely thin contracts (13–25 prints/day, quoted
34.7%) remain excluded, and the open-interest floor is unaffected. The tool
and its output are in the repo — rerun it against any session's audit log.

## Disclaimers

This is a hackathon research project operating exclusively on **simulated (paper) trading**. Nothing in this repository is investment advice or a recommendation to buy or sell any security. Paper-trading results are hypothetical and do not represent actual trading. Options trading involves substantial risk and is not suitable for all investors.

## License

[MIT](LICENSE)
