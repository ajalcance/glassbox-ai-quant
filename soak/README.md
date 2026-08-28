# Soak & chaos harnesses

The guardrails in this repository are claims until something tries to break
them. These are the tools that try, plus the invariant monitor that watches a
live session. They import `glassbox` from the repo, so they exercise the real
code — not a reimplementation of it.

Every tool requires `--account <PAPER_ACCOUNT_NUMBER>` and aborts if the
credentials in `.env` resolve to any other account. There is deliberately no
default: a testing tool with a default account is how one ends up pointed at
the account you cannot afford it to touch.

**Results land in `soak-results/`, which is gitignored.**

---

## `soak_active.py` — concurrency and crash chaos

Places **real but unfillable** orders (far-OTM spreads priced where they rest)
on a paper account, then attacks the pipeline with conditions the market cannot
be relied upon to produce.

```bash
uv run python soak/soak_active.py --account YOUR_PAPER_ACCOUNT      # all scenarios
uv run python soak/soak_active.py --account YOUR_PAPER_ACCOUNT --scenario news_storm
uv run python soak/soak_active.py --list
```

| Scenario | What it attacks |
|---|---|
| `news_storm` | 12 threads deliver the *same* news item simultaneously, then 12 more fire the same `client_order_id` off one barrier. Proves the check-then-act race between the socket and the poller cannot produce two positions |
| `sqlite` | 8 concurrent writers plus the exact cross-thread pattern the trader's news-stream thread uses against its shared store |
| `kill` | Kill switch engaged while an order rests; guards must reach HALT_HARD, the halt must latch after the switch clears, and submissions during rapid switch flips must stay consistent |
| `crash` | `SIGKILL` of a submitting process at three points around order submission; state is rebuilt from the store plus the broker and the idempotent retry must converge on exactly one order |
| `scheduler` | Trader, scheduler, supervisor and a report reader hammering one database and per-role audit chains at once |

It refuses to run while the market is open (`--allow-open` overrides), and
sweeps its own resting orders on exit.

**Two bugs this found, both in code paths the unit tests could not reach:**

- The trader shared one SQLite connection and one `AuditLog` between its main
  loop and its news-stream thread. Because a `sqlite3` connection is bound to
  its creating thread, every socket-delivered story raised inside
  `process_news` — and, having already been marked seen, was then skipped by
  the REST poller. Socket news was being silently lost in production.
- Three writer processes appending to one daily audit file each cached their
  own `prev_hash`, so ordinary concurrency broke the tamper-evident chain.
  Fixed by giving each process its own chain (`YYYY-MM-DD-<role>.jsonl`).

## `soak_infra.py` — infrastructure chaos

Runs **on the host**, as root, with the market closed. Pure standard library;
`glassbox`-level verification happens inside the trader container via
`docker exec`, so it checks the deployed code.

```bash
scp soak/soak_infra.py root@YOUR_SERVER:/root/
ssh root@YOUR_SERVER 'python3 /root/soak_infra.py all'
ssh root@YOUR_SERVER 'python3 /root/soak_infra.py reboot'   # then ssh back and:
ssh root@YOUR_SERVER 'python3 /root/soak_infra.py verify'
```

| Scenario | What it attacks |
|---|---|
| `kill-containers` | Kills each container's main process **from the host namespace** — the only faithful crash. `docker kill` is an operator stop that restart policies deliberately ignore, and `kill -9 1` inside a container does nothing, because the kernel ignores SIGKILL aimed at PID 1 from within its own namespace |
| `docker-restart` | Restarts the Docker daemon under the running stack |
| `partition` | Drops **all** outbound HTTPS for a few minutes. The supervisor must keep evaluating guards throughout — only true because every broker call carries an explicit timeout |
| `reboot` / `verify` | Reboots the host, then sweeps containers, chains, supervisor freshness, disk, NTP, and leftover firewall rules |

The partition scenario installs a failsafe that removes its own firewall rule
even if the harness dies mid-run.

## `monitor_passive.py` — read-only invariant monitor

Runs alongside a live session and never writes to production state. State is
read through a consistent SQLite snapshot taken from a read-only connection
(`--source docker` snapshots it out of the container, since compose uses named
volumes).

```bash
uv run python soak/monitor_passive.py --account YOUR_PAPER_ACCOUNT
uv run python soak/monitor_passive.py --account YOUR_PAPER_ACCOUNT --source docker
```

Each cycle it verifies: every per-role audit chain; that local open positions
agree with the broker; that stored heat equals the sum of open `max_loss` and
sits under the cap; that no live `client_order_id` is duplicated and no unknown
`gbx-*` order exists at the broker; that position status only moves along legal
transitions; that the trader heartbeat is fresh while the market is open; that
containers are up; and — because a hung process still looks "running" to
Docker — that the supervisor has logged recently.

It writes a findings log, a `samples.jsonl` time series (equity, heat,
positions, drawdown, container memory, disk), and serves a localhost SSE page
with an equity sparkline. `--ntfy-topic` sends CRITICAL/FAIL findings to a
phone: a halt at 2am that only a local file knows about is a halt nobody acts
on.

---

## Results this produced

Run against the live paper stack the weekend before the contest:

- **Concurrency**: exactly one broker order in every duplicate-storm round.
- **Crash recovery**: `SIGKILL` at three timings, all converged on one order
  with a clean reconcile.
- **Container crashes**: all five revived by restart policy, chains intact.
- **Network partition**: supervisor kept guarding throughout and recovered
  within seconds.
- **Host reboot**: the entire stack, monitor included, returned unattended.

The audit chains verified after every scenario. See the repository README for
the bugs the drill suite caught, including a flatten that left one leg of a
spread on the book at exactly the moment the guards had decided everything must
go.
