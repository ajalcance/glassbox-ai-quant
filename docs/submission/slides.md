# Slide direction — GlassBox AI Quant

**9 slides** (title + 7 content + close). PDF, no count limit stated by lablab.
Paired with the 3-minute video script in `video-script.md` — the video walks
these in order, so keep the sequence.

## Design system

Reuse the dashboard's identity so the deck and the product look like one thing.

| Token | Value | Use |
|---|---|---|
| Ground | `#0B1014` | Slide background |
| Panel | `#111820` | Cards, code blocks |
| Ink | `#E4EBF2` | Body text |
| Muted | `#8B98A5` | Labels, captions |
| Accent | `#56C8B8` | One highlight per slide — never two |
| Pass | `#46C98B` | PASS chips only |
| Veto | `#E5685C` | VETO chips only |

- **Type:** one sans for headings, monospace for every number, ticker and
  identifier. Numbers in monospace is what makes a deck read as engineering.
- **Rule:** one idea per slide. If a slide needs a paragraph, it's two slides.
- **Green and red are reserved** for pass/veto semantics. Never decorative.
- 16:9. Dark throughout — it matches the product and the screenshots sit in it
  without a jarring seam.

---

## Slide 1 — Title

**GlassBox AI Quant**
*An autonomous options agent that trades mispricing, not headlines*

- Built solo on Alpaca · Alpaca AI Trading Agents Hackathon 2026
- Small: paper trading · Python · 400+ tests
- Visual: the dashboard's dark ground; the word **Box** in accent, matching the
  product wordmark.

---

## Slide 2 — The thesis

> ## The news isn't the signal. The mispricing is.

Three lines, generous spacing:

- The options market already publishes how far it expects a stock to move
- Good news that's fully priced in is not an opportunity
- **The only tradable question: bigger or smaller than what's priced?**

Visual: a single number line — `implied 2.65%` on one side, `expected 5.2%` on
the other, accent arrow between them.

---

## Slide 3 — How it works

Vertical pipeline, each stage a thin panel card. Show attrition — the funnel
narrowing is the story:

```
Alpaca News WebSocket                    ~300 stories/day
   ↓  deterministic filter                 kills ~95%
LLM analyst → JSON estimate only          direction, magnitude, horizon
   ↓  EDGE TEST vs ATM straddle
   expected ≫ implied → buy convexity
   expected ≪ implied → sell premium
   expected ≈ implied → NO TRADE          ← most of the time
   ↓
defined-risk structure → sizing → 18 gate checks → atomic multi-leg order
```

Caption, muted: *the LLM estimates a magnitude; it never names a strategy and
never places an order.*

---

## Slide 4 — The invariant

Full-bleed, one sentence, large:

> ## ML sets selection and sizing.
> ## Deterministic code sets limits.

Beneath, small: *A model failure shrinks a position. It can never breach a risk
cap.*

This is the slide a judge screenshots. Give it room — nothing else on it.

---

## Slide 5 — Risk is structural, not advisory

Two columns.

**Left — every position is defined-risk**
- Worst case computed *before* entry, from real strikes
- No naked options, ever — asserted per leg at the wire
- Atomic multi-leg orders; deterministic `client_order_id` so a retry can never
  double-fill

**Right — 18 ordered gate checks**
Render as a compact grid of monospace chips: `kill_switch` `market_window`
`session_room` `defined_risk` `position_size` `portfolio_heat` `greeks_bands`
`concentration` `correlation` `liquidity` `time_to_expiry` `macro_blackout`
`corporate_action` `daily_loss` `max_drawdown` `rate_limit` `duplicate`

Footer: *a pure function — no I/O, exhaustively testable, impossible to bypass.*

---

## Slide 6 — What a brokerage account can't show you

**Hero screenshot slide.** The vetoed decision from the dashboard, with the red
`correlation` and `liquidity` chips clearly legible. Crop tight; don't shrink
the chips to fit more.

Overlay, one line:

> An order that was never submitted leaves no trace at the broker.
> It exists only because we logged the decision not to place it.

Small stat strip beneath: `[N] evaluated · [N] refused · [N] traded`

---

## Slide 7 — Learning within its sample size

Three cards, each with the honest constraint stated:

| Component | Method | Why this and not the obvious choice |
|---|---|---|
| **Meta-labeler** | Regularised logistic regression | A boosted ensemble on 40 rows is noise fitted with conviction. **Abstains below 30 outcomes** and says so |
| **Structure selector** | Thompson-sampling bandit | Policy gradient needs ~10⁵ episodes; a contest yields tens |
| **Volatility model** | HAR-RV, frozen pre-contest | Trained on 3,784 observations. Loadings rise with horizon and sum below 1 — the textbook result |

Caption: *models chosen for the sample size we actually have, not for
sophistication.*

---

## Slide 8 — Built end to end on Alpaca

Two columns, logos/labels in monospace.

**Used**
- Trading API — atomic multi-leg options orders
- Market Data API — chains, quotes, Greeks
- **News API** — the WebSocket the whole signal runs on
- **MCP Server** — human inspection layer (verified: 72 tools)
- **CLI** — independent cross-check on the SDK's account view

**Deliberately not built**
- No backtester — an LLM with a 2025 cutoff has *seen* these moves; a backtest
  is memory, not prediction
- No deep RL, no martingale, no naked options, no parameter fitting on contest
  data

Footer: *the honest gap beats a fabricated Sharpe ratio.*

---

## Slide 9 — Close

**Results** (fill from the audit log — never estimate):
- `[N]` trades · `[P&L]` · `[N]` refusals logged
- Caveat in muted text, deliberately visible: *four sessions is noise — judge
  the process*

**Repo:** github.com/ajalcance/glassbox-ai-quant
**Demo:** `[dashboard URL]`

Closing line, accent:

> **Alpaca shows what happened. GlassBox shows why — and what it refused to do.**

Thanks to @AlpacaHQ and @lablab.ai.

---

## Build notes

- **Export PDF at 16:9**, fonts embedded. Check it on a phone — judges skim on
  mobile.
- **Slides 4 and 6 carry the deck.** If time is short, polish those two and let
  the rest be plain.
- Every bracketed number comes from `make calibrate` or the nightly report.
  A wrong number on a slide undoes the credibility every other slide builds.
- Screenshots: capture at 2× and downscale, so text stays crisp in the PDF.
