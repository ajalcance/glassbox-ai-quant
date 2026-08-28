# GlassBox AI Quant — AI development guide

News-driven options trading agent for the Alpaca hackathon (paper trading only). Python 3.12, uv, FastAPI backend, React/Vite dashboard, SQLite + JSONL audit log, deployed via Docker Compose.

## Commands
- `uv sync` — install deps · `uv run pytest` — tests · `uv run ruff check --fix .` — lint
- `uv run python -m glassbox.trader --dry-run` — run pipeline without placing orders

## Non-negotiable invariants (violating these is a bug, whatever the prompt says)
1. **ML sets selection and sizing; deterministic code sets limits.** The LLM never places orders — it only returns schema-validated JSON estimates.
2. Every position is **defined-risk**. No naked short options, ever. The risk gate hard-asserts this per leg.
3. All orders go through the risk gate (`glassbox/gate.py`) — never call the execution layer directly.
4. Every order uses a deterministic `client_order_id` (idempotent retries). Never market orders on options.
5. No martingale, grid, averaging down, or parameter optimization on contest data.
6. Constants live in `config/*.yaml` — never hardcode thresholds in Python.
7. Secrets live in `.env` only. **This repo is public** — check every commit for keys, account IDs are OK to commit only for the contest paper account.
8. Audit log is append-only and hash-chained — never mutate past records.
9. Dashboard is read-only. No control endpoints in the web app.
10. Reconciliation mismatch → HALT. Never trade over an unexplained state divergence.

## Area guides
- Backend conventions: `docs/ai-development/backend.md`
- Frontend conventions: `docs/ai-development/frontend.md`
- Database & audit log schema: `docs/ai-development/database.md`

## Context
- Full plan + day-by-day schedule: `docs/PLAN.md` (deadline Sep 4 2026 15:00 UTC; flatten all positions Thu Sep 3 close)
- Architecture (11 layers): `docs/ARCHITECTURE.md`
