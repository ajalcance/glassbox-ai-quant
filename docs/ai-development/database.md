# Database & audit log (AI development guide)

## SQLite (WAL mode) — `data/glassbox.db`
Operational state, rebuildable from broker + audit log at any time. Broker is truth; this is a cache.

Tables (keep this file in sync when schema changes):
- `positions` — open/closed positions: structure, legs, entry, barriers, status
- `orders` — client_order_id (PK), alpaca_order_id, state machine status, fills
- `signals` — one row per news event that survived the filter, with LLM output + edge test
- `training_rows` — triple-barrier-labeled outcomes feeding the meta-labeler
- `bandit_state` — per-arm posterior parameters, updated on position close
- `universe` — daily recomputed tradable symbols with liquidity metrics

Rules: migrations as numbered SQL files in `migrations/`, applied at boot; every table has `created_at`; writes go through a single repository module (`glassbox/store.py`) — no raw SQL scattered in business logic.

## Audit log — `audit/YYYY-MM-DD-<role>.jsonl` (append-only)
One JSON record per decision. Each record embeds `prev_hash` (SHA-256 of the previous record in the same file) — tamper-evident chain. NEVER rewrite a line; corrections are new records referencing the old `record_id`.

One file per UTC day **per writing process** (`trader`, `supervisor`, `scheduler`, plus utility roles like `drills`/`smoke`). Each process owns its file exclusively — this is what keeps the chains valid under concurrency, since every writer caches its own `prev_hash`. A day is verified by checking every role's chain independently (`glassbox.audit.verify_day`). Readers merge all roles' records by timestamp. Legacy pre-split `YYYY-MM-DD.jsonl` files are still read and verified.

Record shape: `record_id, ts, prev_hash, kind` + kind-specific payload:
- `signal` — news event, extracted features, LLM model + prompt hash, rationale, edge test numbers
- `gate` — all 18 check results (including passes), verdict
- `order` / `fill` / `exit` — execution lifecycle with IDs
- `halt` / `resume` — guard events with reason
- `report` — nightly summary pointer

The audit log doubles as the ML training source and the demo material. When in doubt, log more context, never less.
