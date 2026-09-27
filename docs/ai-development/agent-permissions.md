# Agent permissions (AI development guide)

Read this before changing `.claude/`, `deploy/host/`, or anything the coding agent
uses to reach production.

The agent watches the US session while the owner sleeps, so a confirmation prompt
protects nothing: nobody is there to read it. Every action the agent takes either
runs or is refused outright, and the approvals that matter are the owner's own
acts — merging a pull request, adding a label, running a command on the host.

## The layers

| Layer | Where | What it decides |
|---|---|---|
| OS sandbox | `.claude/settings.local.json` (local, generated) | What every shell command can read, write and reach |
| Deny rules | `.claude/settings.json` | Alpaca MCP writes, secrets, the agent's own settings, owner-only git and GitHub actions, a second live trader |
| Hooks | `.claude/hooks/` | What the rule syntax cannot express: ssh arguments, git global options, wrapped commands, secrets in written text, unknown MCP tools |
| Host gate | `deploy/host/` | The only commands the agent's key can run on the production host |

The agent cannot edit any of these; `tests/test_claude_settings.py`,
`tests/test_claude_hooks.py` and `tests/test_gbx_dispatch.py` pin their shape.

## Reaching production

Only as `ssh gbx <verb> <args>`, standing alone:

| Verb | Runs |
|---|---|
| `state` | `gbx-state`: failing, positions, orders, equity, halt, soak failures |
| `checks` | every monitor check with its detail |
| `deployed` | the host's commit and each container's image |
| `tail <svc>` / `logs <svc> <since>` | container output, per-story skip lines dropped, repeats collapsed |
| `daily <YYYY-MM-DD>` / `cohort` | the session and cohort reports |
| `deploy <svc>` | fast-forward to `origin/main`, build, write a `deploy` audit record, restart that one service; refused 09:25–09:45 ET |

Clearing a halt, flattening, the ledger volumes and the chaos lane are the owner's,
through the owner's own key. The client hook and the host dispatcher carry the same
grammar; a test fails if they drift. Adding a verb means changing both, and the
owner re-running `install-agent-access.sh`.

### One-time setup (owner)

On the laptop — a key for the agent only, and the alias the hook insists on:

```
ssh-keygen -t ed25519 -N "" -C gbx-agent -f ~/.ssh/gbx_agent

Host gbx
  HostName 64.177.40.87
  User gbxagent
  IdentityFile ~/.ssh/gbx_agent
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 10
  ServerAliveInterval 30
```

Then, from the laptop with the owner's own key, install it on the host from an
up-to-date checkout of `main` (the `$(cat ...)` expands locally, so the public key
is what travels):

```
ssh root@64.177.40.87 "sudo -u glassbox git -C /home/glassbox/glassbox-ai-quant pull --ff-only && bash /home/glassbox/glassbox-ai-quant/deploy/host/install-agent-access.sh '$(cat ~/.ssh/gbx_agent.pub)'"
```

The script copies the dispatcher to `/usr/local/sbin`, so a later `git pull` never
changes what the agent can run. Every invocation is logged to `/var/log/gbx-agent.log`.

## Changing the rules

The agent drafts; the owner applies. Settings and hooks are copied into place from
the owner's terminal, then committed on a branch like any other change.
