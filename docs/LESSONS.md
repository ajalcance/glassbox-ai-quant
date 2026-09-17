# Lessons from running an options agent live

Written after three weeks of unattended live paper trading (28 Aug – 17 Sep
2026): one hackathon week on the contest paper account `PA31QN3VRL7B`, then a
commercial build with a separate dev/chaos lane on its own paper account.

This is a record of what held and what broke, with the evidence for each. The
design intent is in `ARCHITECTURE.md`; this file is what contact with a live
market did to it. Every claim below is traceable to an audit record, a commit,
or a measurement — the dated findings log is `local/POST-SESSION-REVIEW.md`
§1–28.

The headline number frames everything else: over the contest week the system
realised **−$174**, of which **a single bug cost −$306**. Without that one
defect the week closes **+$132**. The strategy was not the problem. The
engineering was.

---

## 1. The lesson under most of the other lessons

**Code that has run for weeks without incident is not proven code. It may
simply be unreached.**

The −$306 AAPL iron condor was caused by two defects that had been sitting in
`manage.py` since 28 Aug, before the contest started:

- `thesis_complete` counted underlying movement as success for a `vol_only`
  thesis — but only a *long-vol* structure profits from magnitude. For short
  premium every direction is the wrong direction.
- `entry_spot` was recorded at decision time, not fill time, so the move was
  measured from the wrong origin.

Neither was new. What made them reachable was a change on 2 Sep to strike
selection and the entry ladder — because **no iron condor had ever filled
before**. The position opened and closed inside 60 seconds, booking the week's
largest loss on a barrier that reported success.

The corollary is uncomfortable and worth internalising: **a change that
increases the coverage of existing code carries the same risk as new code.**
Improving fill rates does not just get you more fills; it gets you your first
execution of every path that only a fill can reach.

---

## 2. What works

The failures below are more instructive, but they are not the whole picture.
These held under live fire, and each is listed with what tested it.

### The architecture held. The implementation is what broke.

Not one of the three weeks' serious incidents was a design failure. Every one
was a correct component behaving correctly in a context nobody had enumerated:
the gate was right to veto SPY, reconcile was right to halt, the supervisor was
right to flatten, the drawdown guard was right about the dev account. In each
case the *surrounding code* was wrong about what that correctness implied.

That is the best news in this document. Wrong abstractions are expensive to
fix; wrong code is cheap.

### Reconciliation mismatch → HALT

The single most valuable rule in the system. It fired on day one, 56 seconds
after the first-ever live entry, and again after the supervisor's out-of-band
flatten — refusing to trade for three hours over a divergence it could not
explain. **Both times the halt was right and the surrounding code was wrong.**

A system that halts on unexplained state divergence converts silent corruption
into loud downtime. Downtime is survivable.

### Bands, not equality, for in-flight state

An `opening` position with a working entry order may legitimately be absent at
the broker — nothing filled, partly filled, fully filled are all consistent.
Comparing as a *band* rather than demanding equality is what makes the halt rule
usable; equality would halt on every resting order. The same applies on the
close side.

### Severity-ordered exit barriers

Eleven barriers, ordered **obligation → protection → profit-taking**:

```
DEADLINE → EXPIRY_RISK → MACRO_RISK → THESIS_COMPLETE → THESIS_BROKEN
  → STOP → BELL → TRAIL → BREAKEVEN → PROFIT → TIME
```

All have now fired live. The ordering has never produced a wrong precedence in
production — when two barriers were eligible, the more urgent one was always
the right choice.

Realised P&L by barrier across 22 closed positions:

| barrier | n | realised |
|---|---|---|
| bell | 8 | −$286 |
| thesis_complete | 5 | −$248 *(includes the −$306 bug)* |
| stop | 2 | −$221 |
| trail | 2 | **+$111** |
| supervisor_flatten | 2 | −$66 |
| deadline | 1 | +$39 |
| time | 1 | +$14 |
| breakeven | 1 | −$10 |

**What this does and does not say.** It is *not* a ranking — barriers fire on
different situations, and `bell` is negative precisely because it cuts positions
before they have had time to work. It does say that the one barrier built to
*protect* gains (trail) is the only clearly positive one, and that it was added
in response to a specific observed loss (TLT giving back $170 of a +$306 peak).

### The 21-check gate, which does not short-circuit

It has never approved an undefined-risk structure. More usefully: because it
records **every** reason rather than stopping at the first, correct attribution
is possible after the fact. That is what let us discover that a new check
appearing as "the top veto, 9 of 14" had actually *uniquely* blocked only 4.

A gate that short-circuits would have made that question unanswerable.

### The hash-chained audit log — which caught us

It is not decoration. When a repair tool was run with the wrong role while the
trader owned that day's file, the chain forked and **the monitor said so**.
Records 72 and 74 of `2026-09-12-trader.jsonl` share a `prev_hash`, and that
day's chain no longer verifies.

The fork stands. A broken chain is *evidence*; rewriting it to look clean is the
one thing the chain exists to make impossible.

### The chaos soak found production bugs that tests did not

Running the day's race family under 8-thread concurrent `enforce` every 15
minutes on the dev lane surfaced **reconcile clearing halts it did not set** — a
bug with a pathway (supervisor halt → flatten → clean reconcile → guard
evaporates) that no reasonable unit test would have been written to cover.

Standing record: **~2,968 passes, 26 failures all-time.** Of those 26, three
were an Alpaca outage and three were our own experiment leaving positions
behind — the soak's false-positive rate is near zero, which is what makes a red
result worth acting on.

Supporting endurance data: SQLite at 0.8s per 1,200 ops under 8 writers, 654
SIGKILL trials with **0 convergence failures**, 0 monitor gaps in 19,545
samples, trader memory flat at 4%.

### The separate dev lane

Every experiment, drill and destructive test ran on the dev account. The trading
account was never touched by hand. This is what made it *safe* to be aggressive
about chaos testing — and it contained a real incident when our own fill
experiment abandoned 59 spreads and **−$12,767**. On the dev account that is a
finding. On the live account it is a disaster.

### Verifying against live chains before touching production

Liquidity-aware strike selection needed **three** corrections, every one found
by checking against real chains on the dev lane rather than fixtures:

1. a pure liquid pick stretched AVGO's 2.5-point spread to **45 points**;
2. the liquid short then moved *toward* the money (credit shorts may only move
   further OTM);
3. the geometric fallback could still land a short one notch inside target.

Net live result: **3 vetoes converted to passes, 0 passes lost**, every wing at
its intended width.

### Canary tests on hand-maintained data

The macro calendar is hand-maintained on purpose — a verified date beats an API
nobody has exercised — and its known weakness is that it goes stale *silently*:
the blackout simply stops matching and a dangerous week looks like a quiet one.
That already happened once, for five sessions, with nothing saying so.

Two tests and a preflight check were added specifically to detect it. On the
evening of 16 Sep, the moment the last event on the calendar printed, **both
tests failed and the preflight check reported `STALE`.** The system caught its
own known weakness within hours, on the correct day, before the next session.

> A component with a known failure mode needs a detector for *that* mode. It
> will not announce itself.

### Sign conventions, written down once and defended

```
realized = (-fill - entry) * 100 * qty        # positive = paid
```

A debit spread bought at +2.50 whose close fills at −2.47 realises −$0.03 per
share. The naive `(fill - entry)` formula books it as a **$497 loss on a $3
trade**. That is not hypothetical — it is the fill a Friday drill actually
produced, and the reason the convention is spelled out in a comment that is
longer than the code.

### Abstain rather than guess

The meta-labeler refuses to score until it has 30 outcomes. That is correct.
§5 documents the cost of the *fallback*, not of the abstention.

### Defined risk, asserted per leg, twice

Never once in question across three weeks. The one invariant that never needed
defending is the one the whole system is built on.

### Degrade-and-escalate, once we got it right

The final shape of the crash guard is worth keeping: absorb transient failures,
count them, and **withhold the heartbeat once they persist** so the supervisor
takes over deliberately. Transient blips ride through; real outages escalate
once, cleanly. See §6 for why the obvious version of this is a trap.

---

## 3. Things we built that were wrong

### The entry ladder was a second order-origination path, and we treated it as an execution tweak

Shipped 2 Sep to convert resting entries. Afterwards — and only because the
operator pushed back twice, asking *"so you don't see a problem now with the
ladder strategy?"* — we found it:

- **submitted orders through a halt.** `lifecycle.sync` runs *before* `tick()`'s
  halt check, so ladder rungs put new orders on the wire while the system was
  halted. Invariant 10 violated.
- **carried a gate approval 80–160 seconds stale.** Invariant 3 violated.
- calibrated its concession on a quoted spread we already knew was unreliable.
- never asked *why* an order wasn't filling.

> **Any new code that can put an order on the wire is not an execution tweak.
> It is a second order-origination path, and it must answer everything the gate
> answers.**

The gate is only non-bypassable if you have enumerated every path to the broker,
and *"this just re-prices an existing order"* is how a second path gets built
without anyone deciding to build one.

### The observer computed its own P&L — three separate times

Found in the drills (31 Aug), then the hold probe, then the **nightly report**
(`c0b03f6`), which showed `$0.00` for a position that realised `−$10.00`. Each
recomputed P&L from decision-time marks instead of reading the fills.

> **Realised P&L has exactly one source: the fill.** Anything that recomputes it
> will eventually disagree, and it will disagree in the direction of whatever
> the observer wanted to see.

The report was judge-facing. It was wrong for five nights.

### Mid-price marks were a fiction we were managing against

COIN's mid said $0.48 to close; the market wanted $0.58. The break-even barrier
armed on a phantom +$14 peak, fired at a phantom $0, and the real exit cost $10
— **21% of the credit**.

Fixed by marking on the **liquidation side**: long legs at the bid, short legs
at the ask, never the mid. Live data then showed the replacement is
*consistently pessimistic*, which is the right direction to be wrong in:

| position | marked | filled |
|---|---|---|
| SPY debit (4 Sep) | −$81 | **−$77** |
| TLT debit (10 Sep) | −$208 | **−$144** |

Barriers trigger on a conservative number and the exit lands above it.

### Cleanup code handled failure but not success

`soak/fill_experiment.py` cancelled resting **orders** on exit. A filled arm is
not an order — it is a **position**. The experiment abandoned 59 SPY spreads
plus AAPL/QQQ/SMCI legs, **−$12,767**, dragging the dev account to −13%.

Two things fell out of that:

- Cleanup needed **two** passes, because closing short legs first strands their
  longs (the broker rightly refuses a naked short).
- The drawdown guard was **right** and our soak check was wrong.
  `update_peak_equity` reads the broker's 1-month peak precisely so a fresh
  store cannot silently reset it. The assertion conflated "halt latched" with
  "verdict is continue" and had to be split.

> **Write cleanup for the path where the thing works.** Failure cleanup is
> obvious and gets written; success cleanup is the one that leaks.

### Shared mutable state with no ownership

`reconcile.enforce()` cleared the halt flag on any clean reconcile. The
supervisor writes the **same key** for daily-loss and drawdown breaches.

The failure mode is vicious: a supervisor halt is normally accompanied by a
*flatten*, and **a flattened book reconciles perfectly**. The outermost capital
guard would have evaporated on the very next tick — reliably, every time it
fired.

Fixed by tagging halts with a source. Reconcile clears only its own, and an
unrecognised source is never cleared: not knowing who halted is itself a reason
to stay halted.

---

## 4. Things we believed at setup that live trading refuted

### A limit in absolute dollars silently changes meaning

`delta_dollars_band: 40000` was calibrated on 28 Aug against SPY verticals then
carrying $25–35k of delta. SPY moved. By 31 Aug a **single** vertical carried
$34,450, and a two-lot was refused at −$49,657 on an empty book.

SPY — the most liquid underlying in the universe — became effectively
untradeable without anyone changing a setting.

> **Express a limit as a fraction of the thing it constrains.** A constant is a
> calibration with an undocumented expiry date.

### Two correct guards can be jointly wrong

We raised base risk `R` to 1.0% believing *"the delta band caps SPY at a single
spread whatever R says."* False, in an instructive way: **the gate is a binary
veto, not a size reducer.** At R=1.0 the sizer asked for 2 spreads carrying
−$48,119 against a ±$40,000 band, and the trade was refused outright rather than
trimmed to the 1 spread that fits at ~−$24,000.

We reverted R (the risk-*reducing* resolution) rather than widen the band, then
fixed the actual cause — fitting `qty` to the band before asking the gate —
after which R=1.0 was safe.

> **When a sizer feeds a binary veto, they are one component and must be
> designed together.**

### "Not yet" and "not ever" are different answers

The opening-auction skip **discarded** signals, and `_seen_news` then prevented
reprocessing, so a story arriving at minute 11 was never looked at again at
minute 16.

Day one's two strongest signals — edge ratios **1.97 and 1.74**, the best of 157
news items — were lost exactly this way. The skip was right; discarding instead
of deferring was the bug.

### Selection handed the gate candidates that could not pass

One session produced **17 liquidity vetoes** out of 20 gate arrivals, on the
most liquid names in the market: AVGO open interest **4**, MU 37, JPM 63.

`nearest()` chose strikes by distance alone while open interest and spread sat
unused on the quote object. The deeper cause was upstream: **the horizon-driven
expiry pick lands on the nearest weekly**, where OI is thin on non-round
strikes. A 6-hour thesis does not need a 2-day expiry.

> **Fix the cause, not the symptom.** We shipped liquidity-aware selection
> (symptom) and recorded monthly-expiry selection (cause) as the real change.

### Four workarounds for one upstream problem

The free **indicative** options feed reads 5–13× wider than OPRA on single
names. We wrote four separate consumer-side compensations: a liquidity cap tuned
to inflated spreads, liquidation-side marks, a windowed spread median (UBER read
30.8% → <20% → unfillable within minutes), and spread-scaled ladder concessions.

> **When you are writing the third workaround for the same symptom, the problem
> is upstream.**

---

## 5. Measurement lessons

### Backward-looking estimators on forward-looking events

We claimed the sizer is *most conservative on event days*, because `vol_budget`
scales by `target_vol / realised_vol` and event days are volatile. Measured
across three sessions, that was **backwards**:

| day | median realised vol | median fixed budget | median vol budget |
|---|---|---|---|
| 14 Sep (calm) | 2.8% | $389 | $282 |
| 15 Sep (calm) | 3.0% | $377 | $282 |
| **16 Sep (FOMC)** | **1.9%** | $356 | **$388** |

On FOMC day realised vol was the *lowest* of the three and the vol budget the
*largest*. Realised vol is **backward-looking**, and the run-up to a *scheduled*
event is quiet. The volatility arrives after the print.

> **Reason about how a metric is computed, not about the situation it is
> measuring.** We reasoned about the event and not about the estimator.

### Two quantities sharing one scale

`meta_multiplier()` maps a **calibrated P(profitable)** to a size multiplier.
When the meta-labeler abstains it is fed the **LLM analyst's self-reported
confidence** instead. Across 338 scored signals — every one abstaining, the
model has never trained in production:

```
p 0.60 -> 0.50x    200 of 338 (59%)   <- an LLM's round-number hedge
p 0.65 -> 0.60x     62
p >=0.85 -> 1.00x   11 of 338 (3.3%)
```

The multiplier is effectively a **constant 0.5×, not a discriminator**, and
nobody chose it. Compounded with context haircuts, the system runs at **~37% of
base R every day**.

> **A fallback that substitutes a different quantity into the same mapping is
> not neutral.** It is a silent, permanent haircut whose size nobody decided.

### Edge without execution cost is not edge

The edge test compares expected move to implied move. It never subtracted the
cost of getting in and out. Measured across every position that filled:

| profit target ÷ round-trip cost | closed | net P&L |
|---|---|---|
| **below 1.5×** | 4 | **−$690** — every one a loss |
| at or above 1.5× | 12 | +$125 |

Arithmetic, not a pattern found by sorting: *a profit target below the cost of
getting on and off cannot be reached whatever the forecast does.* The worst case
caught live was a spread with a **$144 round-trip cost against a $41 profit
target**. And the −$306 condor sits at **0.27×** — no path to profit
*independently* of the two barrier bugs that closed it.

### Fill rate is a process metric, not a result

We cited fill rate as evidence the ladder worked until the operator asked:
*"we highlight too much on this ladder — maybe this is a problem?"*

He was right. **Filling is not the goal; filling profitably is.** The ladder's
two surviving fills netted +$23 across two observations, which settles nothing.

### What actually drives fills

Controlled experiments on the dev lane, one variable at a time:

- **Price** (mid vs marketable): *refuted* — 3/3 both, ~5s each.
- **Quantity** (1 / 5 / 10): *refuted* — 3/3 all.
- **Symbol**: separates, and the mechanism is **quoted spread**:

| underlying | spread | time to fill |
|---|---|---|
| SPY | 2.2% | 5s |
| QQQ | 2.2% | 18s |
| AAPL | 2.4% | 114s |
| SMCI | 3.5% | 211s |
| ADBE | 13.7% | never |

A flat 4-minute budget therefore **truncates a distribution** rather than
separating fillable orders from unfillable ones.

The honest postscript: extending the budget is only a *partial* fix. WMT (15.0%
spread) filled at 362s where the old rule would have killed it — but UBER (9.8%)
got a 600-second budget twice and never filled. **One conversion in three.**

---

## 6. Operational and infrastructure lessons

### A crash guard without an escalation path is worse than crashing

On 11 Sep Alpaca returned HTTP 500 on `/v2/clock` for ~48 minutes. The call had
no error handling and the main loop had no per-tick guard, so the exception
killed the process **79 times in 48 minutes** — each restart replaying preflight
while two live positions sat unmanaged.

The obvious fix is a catch-all. **The obvious fix is also a trap.** A trader
that swallows every error while still stamping a fresh heartbeat *blinds the
supervisor* — the only component left that can act. That is strictly worse than
crashing, because at least a crash escalates.

The design that works is **absorb transient, escalate persistent**: a
consecutive-failure counter gates the heartbeat. Under the threshold the trader
rides out the blip; over it, the heartbeat goes stale deliberately and the
supervisor takes over.

### An absence that produces no error needs a check that looks for presence

Chain capture — the replay harness's only input — **failed silently for a full
week, then failed again for a completely different reason.**

1. It wrote to `/app/chains` inside the container's writable layer, which
   `docker compose up -d` discards on every deploy. Crash-restarts keep the
   layer, so the 79-restart outage was harmless; two deploys were not.
2. We added a named volume. **Still broken.** Docker seeds a new volume from the
   image's directory — contents *and ownership* — but only when that directory
   exists. `/app/chains` was not in the Dockerfile's `mkdir`, so Docker created
   it **root-owned**, and the container runs as uid 10001. The mount was
   present, correct, and unwritable.

Nothing ever errored: capture swallows its own failures by design ("a full disk
must not stop trading"), and replay reported `no_chain`, which read as *"the
feature is new"* rather than *"the data is being thrown away."*

The presence check we added alongside the volume is the only part that did its
job — red four minutes after the open. Which produced a sub-lesson:

> **A check that cries wolf every morning is one you stop reading.** The first
> version keyed off the market clock, so it was red at 09:31 daily before any
> decision had fetched a chain. It now keys off analyst reads. That
> daily-false-alarm pattern is *exactly* how the original failure survived a
> week.

### Every out-of-band action needs a supported repair

The supervisor's emergency flatten talks straight to the broker — it must, since
the reason it is firing is that the trader can no longer be trusted. The cost is
a divergence: positions gone at the broker, still `open` in the store, reconcile
halting on `local-only`.

That halt is **correct**. What was missing was a supported way to *explain* the
divergence, so for three hours the only options were leave the system halted or
hand-edit the production database.

`glassbox/repair.py` rebuilds each exit from the broker's own fill records, in
the live path's sign convention. It refuses a partial flatten, and refuses
positions whose legs are shared with another position because fills cannot then
be attributed. Repaired closes carry **no meta-label** — an infrastructure
failure is not a trading outcome and must never train the model.

### A guard should act once per breach, not once per tick

While the trader crash-looped, the supervisor's stale-heartbeat verdict fired
**eight times in two minutes** — eight breach records, eight flattens, seven of
which closed nothing. Harmless, but it buries the record that matters.

Escalation still has to work both ways: a *different* reason is a new breach and
must act, and inventory reappearing under a standing halt must still be
flattened. The halt's promise is an empty book, not a written-down flag.

### In-memory state has a measurable deploy cost

`_seen_news`, `_deferred` and `_spread_history` do not survive a restart. That
turns a routine deploy into a decision: restarting inside the opening-auction
window discards the deferral queue — the same queue that exists because losing
those signals cost us day one's two best trades.

The practice that emerged: **build first** (does not touch the running
container), hold the restart until past the opening window, restart the trader
**only** so the supervisor keeps watching. Measured restart gap: **5.4 seconds**
against the supervisor's 90-second threshold — a number that was sitting in
`trader_stop`/`trader_start` pairs the whole time rather than needing a guess.

---

## 7. Testing lessons

### 407 tests missed both of day one's bugs

Two genuine bugs halted the system 56 seconds after the first live entry. The
suite was large and green. It missed both for nameable reasons:

- **The stub router's `cancel()` didn't update the order row the way the real
  one does**, so the orphan's birth condition was unreachable in tests.
- **The reconcile test wrote the order row without the position row.** The
  trader writes both.

> **A stub that is kinder than reality tests a system you do not run.**

### Fixtures encode what you imagined; live data contains what is real

None of the unit fixtures contained the weekly-chain OI pattern that produced
all three strike-selection corrections in §2. Verifying against live chains on
the dev lane is what caught them.

### Do not couple behavioural tests to operational data

Refreshing the macro calendar on 16 Sep broke **six** tests at once. They were
asserting blackout *mechanism* — does the window open two hours early, does the
lookahead see a premarket print — against whichever releases happened to be in
the live config.

> **A suite that breaks when you update operational data creates pressure not to
> update it** — on precisely the data that must be kept current.

The mechanism is timeless; the schedule is not. Those tests now use a fixed
fixture calendar, and only the staleness canary reads the live config. Note the
two failure modes are opposites and both matter: the canary *must* read live
data, and the mechanism tests must *not*.

### An over-specified assertion is a failing test that is wrong

Our soak asserted `action == "continue"` **and** `halt latched`. When the dev
account legitimately sat at −13% drawdown the verdict was correctly *not*
continue, and the check failed — on correct system behaviour. It now asserts the
latch only, and records the verdict as informational.

---

## 8. Process lessons

### Record during, decide after

`local/POST-SESSION-REVIEW.md` holds observations made *during* live sessions
for review *after* them. The rule it protects:

> **Bug fixes during a live window: yes. Threshold changes to obtain trades:
> no.** Retuning a limit because it vetoed a trade you wanted is parameter
> optimisation on live data.

That held for the contest week with exactly one deliberate exception —
`vrp_min_for_credit` 0.90 → 0.72 — made on explicit repeated operator
instruction after the conflict was raised twice, and **logged as a threshold
change rather than disguised as a fix**. The measurement that picked *that* knob
is recorded with it: the confidence floor was measured *not* binding (all six
refusals would have died on VRP one stage later), while VRP was refusing a
cluster that missed by 0.13–0.16.

### Verify against code and audit, never memory

The concrete form: `uv run pytest -q`, `ruff` clean, grep-verify the wiring
before claiming something is connected. And when a number matters, **go and
measure it** — restart gaps, ratio distributions, veto attribution, chain
counts, realised vol on event days. Each of these was assumed at some point, and
each was wrong.

### Distinguish "uniquely caused" from "co-occurred"

We reported a new gate check as "the top veto, 9 of 14." True and misleading:
five of those nine were *already* refused by other checks. It uniquely blocked
**4 of 14**; across four sessions its marginal cost was **6 of 40**.

### Say n, and say what the evidence cannot settle

A recurring failure mode, corrected repeatedly:

- *"Fills happen inside the first minute or not at all"* (5 Sep) → refuted four
  days later by AAPL at 114s and SMCI at 211s.
- WMT's 362-second fill called *"first direct confirmation"* → retracted the
  same day when UBER failed twice with a longer budget. **Mixed, not
  confirmatory.**
- The vol-budget claim in §5 — stated confidently, measured backwards.

> **One observation is not confirmation.** State the sample size, and state
> plainly what the data cannot answer.

### Calibrate on the trade-off, not on the maximum

Asked to calibrate the cost-to-trade floor, the in-sample curve said 2.5× beat
1.5× (+$85 vs +$40 kept P&L, 86% vs 64% win rate). We **kept 1.5×**.

The entire gain was four trades netting −$45, averaging −$11 each on a book
whose trades span −$306 to +$85. That is noise, and selecting the maximum over
15 observations is how you fit it.

What *did* support the floor was a robustness property rather than a maximum:
across three independent sessions, **nothing has ever priced between 1.43× and
1.64×**, and 1.5 sits in the middle of that empty band. The value is currently
insensitive — an argument for leaving it alone, with a specific signal to watch
for (candidates stacking either side) that would mean it needs redoing.

### Push back twice, then record the decision

Two of the most valuable changes came from the operator rejecting the first
answer:

- *"so you don't see a problem now with the ladder strategy?"* — asked twice,
  and the second asking found two invariant violations.
- *"why don't you make a research so your knowledge is updated?"* — after we
  claimed we could not know future macro dates while holding working web tools.
  The refreshed calendar caused the bell gate's first-ever production firing six
  hours later.

Where the operator overrode a stated concern, the decision and its rationale
were written down at the time — which is what makes them reviewable now instead
of arguable.

---

## 9. If we started again

1. **Express every limit relative to what it constrains.** No absolute-dollar
   thresholds.
2. **One state-transition path** (`store.transition(pos, from→to, reason)`) that
   refuses illegal edges and audits legal ones. Three of the worst bugs were all
   "a transition landed where its resolver could not see it."
3. **Make the single-writer tick explicit.** The reconcile bands, orphan sweep
   and write-ahead rows are correct *only* because one loop runs
   sync → enforce → manage in that order — and nothing enforces it. The chaos
   soak needed a `tick_lock` to model an invariant that lives in one loop and in
   people's heads.
4. **Subtract execution cost before calling anything edge**, from the first
   version of the edge test.
5. **Capture the inputs to every decision from day one, and check the capture is
   landing.** Two weeks of chain data were lost to a bug that never raised an
   error.
6. **Pay for the real market-data feed.** Four separate workarounds were one
   upstream problem.
7. **Enumerate every path to the broker** and make each answer the gate.
8. **Build the replay harness before the strategy.** The most valuable tool here
   turns the audit log into a regression suite — and every question worth
   arguing about (thresholds, stops, fills) turned out to be a replay question
   rather than a judgement call.

---

## Current state

| | |
|---|---|
| Tests | 516 passing, `ruff` clean |
| Gate | 21 checks, non-bypassable |
| Barriers | 11, all fired live |
| Soak | ~2,968 passes / 26 failures all-time, every 15 min on the dev lane |
| Closed positions | 22, realised −$667 |
| Meta-labeler | abstaining, 20 of 30 outcomes |

Edge remains **unproven on ~20 closed trades**. That sentence belongs in any
conversation about sizing up or going live with real money, and it is the reason
item 4 of the backlog (the ~37%-of-base-R haircut) is gated on item 3
(establishing edge) rather than fixed first.

---

*Sources: `local/POST-SESSION-REVIEW.md` §1–28 (dated findings, recorded live),
the hash-chained audit log, and the commits referenced inline.*
