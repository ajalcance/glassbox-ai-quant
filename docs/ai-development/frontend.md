# Frontend conventions (AI development guide)

Read this before touching `dashboard/`.

## Stack
React 18 + Vite + Tailwind. Charts: TradingView `lightweight-charts` for the equity curve, plain SVG/Recharts for small panels. Data: REST for snapshots, **SSE** (`/api/stream`) for live decision feed — no websocket client, no state library; React context + reducers are enough.

## Rules
- **Read-only, always.** No buttons that mutate server state. No auth. If a control seems needed, it belongs in the CLI.
- Single page, panel grid: equity curve · open positions w/ Greeks · portfolio heat gauge · Greeks bands · live decision feed (the centerpiece — render every gate check PASS/VETO) · bandit posteriors · system status strip · daily reports.
- Terminal aesthetic: dark ground, tabular-nums for all figures, green/red reserved strictly for P&L semantics — status uses its own hues.
- Every panel renders sanely with zero data (pre-market, fresh boot). Empty states are designed, not accidental.
- Timestamps: store UTC, display both ET (market) and viewer-local.
- Keep the bundle lean — no component libraries, no moment.js (use Intl), no icon packs beyond a handful of inline SVGs.
