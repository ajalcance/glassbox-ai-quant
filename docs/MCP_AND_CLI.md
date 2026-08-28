# Alpaca MCP server and CLI

The hackathon requires a project to use Alpaca's MCP server **or** its CLI tools
alongside the Trading API. GlassBox uses **MCP as its primary integration**, with
the CLI as an additional, best-effort cross-check.

## MCP — verified, and the one we rely on

`.mcp.json` in the repo root points at Alpaca's official MCP server. Verified
rather than assumed:

```bash
uv run python -m glassbox.verify_mcp
```

```
  ok    connected to Alpaca MCP Server 3.4.7
  ok    72 tools exposed
          get_account_info, get_all_positions, get_option_contracts, ...
MCP VERIFY PASSED
```

That check speaks the MCP handshake over stdio directly and asserts the account
and position tools are present. A `.mcp.json` nobody has exercised is a claim,
not an integration — and this is the requirement eligibility rests on.

### Using it

```bash
export ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=...
claude          # Claude Code reads .mcp.json from the project root
```

Then ask directly: *"what positions am I holding?"*, *"show me the SPY option
chain for next Friday"*, *"what was my last filled order?"*

### The agent never trades through it

The MCP server exposes 72 tools, including `close_all_positions`,
`cancel_all_orders` and `exercise_options_position`. It is fully capable of
trading — and the agent deliberately does not use it for that.

Every order goes through the deterministic risk gate. A natural-language
interface that could place trades would be a hole straight through the layer the
entire design exists to enforce. MCP is how a **person inspects** the account the
agent trades, which is a different job.

## CLI — an additional cross-check, not a dependency

`glassbox/alpaca_cli.py` uses the Alpaca CLI as a **second, independent source of
truth** for the nightly report.

Reconciliation already compares local state against the broker, but both sides
of that comparison arrive through the same client. If that client is
misconfigured, paginating short, or serving a cached response, the check passes
while being wrong. The CLI is a different binary with its own code path and
auth, so a disagreement means one of the two views is genuinely wrong.

### A measured caveat

Alpaca's CLI is v0.0.13 and labelled alpha. Measured from the development
machine, roughly **one call in six** completes; the rest lose the TLS handshake,
with or without spacing between retries. This may well be local — a datacenter
network could behave completely differently, and the same check should be re-run
on the deployment host before drawing any conclusion about the tool itself.

Either way, the integration is built for that possibility:

- it runs at **report time only**, never in the trading path;
- it retries, then gives up rather than blocking;
- it reports **unavailable** when it cannot run — never silent agreement.

A verification that reports success when it never ran is worse than none, so an
unavailable cross-check is recorded as unavailable and the report says so.
