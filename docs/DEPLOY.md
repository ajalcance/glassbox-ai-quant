# Deployment

Three processes, one image. The supervisor runs in its own container with its
own broker session — if the trader wedges or is OOM-killed, the supervisor can
still reach Alpaca to flatten the book. Collapsing them into one container would
give up the property the whole guard design depends on.

## Run it anywhere

```bash
cp .env.example .env        # fill in Alpaca + Fireworks keys
docker compose up -d --build
docker compose logs -f
```

The dashboard is then on `http://localhost` (Caddy) or `:8847` directly.

This works identically on a laptop and on a server. Nothing in the stack assumes
cloud.

## Going live

The trader starts in **dry-run** on purpose — it runs the full pipeline against
live data and places nothing. Promoting it to live trading is a deliberate act:

```bash
echo "TRADER_MODE=" >> .env      # empty = live paper trading
docker compose up -d trader
```

Watch a full session in dry-run first. A system that has never seen live market
data going straight to live orders is how day one becomes day zero.

## Kill switch

```bash
make kill      # touch data/KILL — supervisor flattens and halts within ~15s
make resume    # rm data/KILL
```

The switch is a file, not an API call, so it works even when the trader is
unresponsive. The supervisor polls for it independently. It lives at
`data/KILL` because that directory is the shared `state` volume in the compose
stack: one touch is seen by every container as well as host-run processes
(`make kill` writes both the host file and, when the stack is up, the volume).

## On a server (optional)

Any Ubuntu 24.04 box with Docker. A DigitalOcean 2 GB droplet is about $12/mo
billed hourly — roughly $3 for a contest week. Use **NYC** for latency to Alpaca.

```bash
# on the droplet, as a non-root user with docker installed
git clone https://github.com/ajalcance/glassbox-ai-quant.git
cd glassbox-ai-quant
cp .env.example .env && nano .env          # keys, and SITE_ADDRESS=your.domain
docker compose up -d --build
```

Firewall — only these three ports need to be open:

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
```

Set `SITE_ADDRESS` to a domain and Caddy provisions TLS automatically. Leave it
as `:80` for a bare IP.

## Scheduled jobs

The `scheduler` service runs the nightly report at 16:15 ET and refits the
meta-labeler at 16:25 ET, weekdays only. Deliberately not cron: cron in a
container loses the environment and the logs, and every time here is a *market*
time — the operator is in Philippine time and the container is UTC, so a job
expressed in either would fire on the wrong day. Jobs are declared in US Eastern
and resolved through the same clock module the trader uses. **Do not set a
container TZ to "fix" this.**

Each job runs at most once per market day, tracked in the store, so a restart at
20:00 does not re-run something that already fired at 16:15.

```bash
make report                                   # force today's report now
uv run python -m glassbox.scheduler --once    # run anything currently due
```

## Models

Models are trained on your machine and shipped as read-only artifacts —
`models/` is mounted `:ro` so a container cannot rewrite its own model.

```bash
make train                                  # locally
docker compose restart trader dashboard     # pick up new artifacts
```

`harrv.json` is committed (four coefficients, reproducible). `metalabel.pkl` is
not — it is regenerated from closed positions and changes nightly.

## Operational notes

- **Data survives restarts.** `data/` and `audit/` are named volumes, so
  `docker compose down` does not lose the audit trail or position state.
- **Logs are capped** at 10 MB × 3 files per service; an overnight run cannot
  fill the disk.
- **Containers run as UID 10001**, never root.
- **The proxy rejects non-GET methods.** The dashboard has no write routes, and
  the edge enforces that independently — two places would have to change before
  the public surface could mutate anything.
- **Timezone.** The container clock is UTC; every date-sensitive decision goes
  through `glassbox.clock`, which uses US Eastern. Do not "fix" this by setting
  a container TZ.
