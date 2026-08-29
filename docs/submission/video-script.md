# Video script — GlassBox AI Quant

**Target 3:00** (limit is 5:00, MP4, under 300MB). ~450 spoken words at a
natural pace. Read it slightly slower than feels right; nerves speed everyone up.

**Structure follows lablab's recommendation:** introduce → walk the slides →
demonstrate the product.

**Before recording**
- Fill every `[BRACKET]` with a real number from the audit log. Never guess one.
- Have the dashboard open with a real session's data, and a **vetoed decision
  visible in the feed** — that shot is the whole video.
- Record screen at 1080p+. Hide bookmarks, notifications, and any window
  showing an API key.
- Say numbers plainly. No hype adjectives — the material is strong enough.

---

## 0:00 – 0:18 · Cold open
**On screen:** Slide 1 (title)

> Most trading bots ask whether the news is good or bad.
>
> This one asks a different question: is the move I expect bigger or smaller
> than the move the options are already pricing?
>
> I'm Albert. This is GlassBox AI Quant — an autonomous options agent built on
> Alpaca, and I built it solo in seven days.

---

## 0:18 – 0:42 · The thesis
**On screen:** Slide 2

> Here's the idea. Options markets constantly publish a number: how far they
> expect a stock to move. You can read it straight off an at-the-money
> straddle.
>
> Good news that's already priced in is not an opportunity — you'd be paying
> full price for something everyone knows.
>
> So the AI reads the story and estimates a magnitude. Then deterministic code
> compares that to what the market already expects. Same headline, opposite
> trade, depending on whether the market is asleep or panicking.

---

## 0:42 – 1:05 · How it works
**On screen:** Slide 3 (pipeline)

> News arrives on Alpaca's WebSocket. A deterministic filter kills about 95% at
> no model cost — boilerplate, stale stories, restatements.
>
> Survivors go to the language model, which returns only a schema-validated
> estimate. Direction, magnitude, horizon. It never names a strategy and it
> never places an order.
>
> Code does the rest: compares against the straddle, picks a defined-risk
> structure, sizes it, and runs eighteen risk checks.

---

## 1:05 – 1:25 · The invariant
**On screen:** Slide 4

> That split is the whole design, and it's one sentence:
>
> **Machine learning sets selection and sizing. Deterministic code sets
> limits.**
>
> A model failure shrinks a position. It can never breach a risk cap. Every
> position is defined-risk — I calculate the worst case before entry, and if I
> can't calculate it, I don't trade.

---

## 1:25 – 2:15 · Live demo — the centrepiece
**On screen:** Switch to the live dashboard. Scroll slowly. Cursor moves
deliberately; let each thing land before moving on.

> This is the dashboard. It deliberately doesn't show equity or positions —
> that's Alpaca's job, and I link to it. This shows what a brokerage account
> can't.
>
> *(hover the decision feed)* Every decision the agent made. Here's a story,
> the model's estimate, what the options implied, and the ratio between them.
>
> *(scroll to a VETOED row — pause here)* And here's the important one. This
> signal had a real edge. The agent wanted to take it. It was refused — by the
> correlation check and the liquidity check, both shown right here.
>
> That record doesn't exist at the broker. An order that was never submitted
> leaves no trace anywhere. It only exists because I logged the decision *not*
> to place it.
>
> *(pan to Refusals panel)* Over this session: [N] signals evaluated,
> [N] refused, [N] reached the market.

---

## 2:15 – 2:40 · Alpaca stack + honesty
**On screen:** Slide 8, then Slide 9

> It's built end to end on Alpaca: Trading API, Market Data, the News API,
> multi-leg options orders, the MCP server for human inspection, and the CLI as
> an independent cross-check on the account.
>
> And there's no backtester — deliberately. A language model with a 2025
> training cutoff has already seen how these stocks moved. A backtest would be
> the model remembering, not predicting. I'd rather ship an honest gap than a
> fabricated Sharpe ratio.

---

## 2:40 – 3:00 · Close
**On screen:** Slide 9 (repo + demo URL visible)

> Over the contest the agent placed [N] trades and finished at [P&L]. Four
> sessions is noise, and I won't pretend otherwise — judge the process, not the
> number.
>
> Four hundred tests, a hash-chained audit log, and a risk gate that can't be
> bypassed.
>
> Alpaca shows you what happened. GlassBox shows you why — and what it refused
> to do. Thanks for watching.

---

## Recording notes

- **The veto shot is the video.** If you only get one thing crisp, make it
  that. Consider a short zoom on the red check chips.
- **Don't rush 1:25–2:15.** It's half the value; give it the time.
- If a live session is quiet and the feed looks thin, that's still fine —
  narrate it: *"most news is fairly priced, so most of what you see here is the
  system declining."* That's the thesis, not a failure.
- End recording on the dashboard, not on your face — the last frame should be
  the product.
