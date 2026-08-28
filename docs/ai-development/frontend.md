# Frontend conventions (AI development guide)

Read this before touching `dashboard/`.

## Stack
React 18 + Vite + Tailwind. Small inline SVG sparklines only — no charting
library. Data: REST for snapshots, **SSE** (`/api/stream`) for the live decision
feed. No websocket client, no state library; React context + reducers suffice.

## Scope rule — do not rebuild the broker's UI
Alpaca's dashboard already shows the equity curve, positions, P&L and order
blotter. We show only what lives in our audit log and cannot exist broker-side.
If a panel could be screenshotted from Alpaca, it does not belong here. Link out
to the Alpaca dashboard instead.

## Rules
- **Read-only, always.** No buttons that mutate server state. No auth. If a control seems needed, it belongs in the CLI.
- Single page, panel grid:
  1. **Decision feed** (centerpiece) — news event, LLM extraction, expected vs
     implied move, and every gate check rendered PASS/VETO
  2. **Veto log** — trades never submitted, grouped by which check rejected them
  3. **Risk budget** — heat vs cap, delta-dollars vs band, R utilization
  4. **Learning state** — bandit posteriors per arm, meta-labeler P(profit)
  5. **System health** — websocket, circuit breaker, reconciliation, supervisor
     heartbeat, halt reason, audit-chain verification
  6. **P&L by bandit arm** — attribution, not raw equity
  7. **Nightly reports**
  Plus a persistent "View account on Alpaca" link.
- Terminal aesthetic: dark ground, tabular-nums for all figures, green/red reserved strictly for P&L semantics — status uses its own hues.
- Every panel renders sanely with zero data (pre-market, fresh boot). Empty states are designed, not accidental.
- Timestamps: store UTC, display both ET (market) and viewer-local.
- Keep the bundle lean — no component libraries, no moment.js (use Intl), no icon packs beyond a handful of inline SVGs.
