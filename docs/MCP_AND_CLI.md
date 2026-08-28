# Alpaca MCP server and CLI

The hackathon requires that a project use Alpaca's MCP server or its CLI tools
in addition to the Trading API. GlassBox uses **both**, each where it actually
fits, rather than adding one as a formality.

## MCP server — the interactive surface

`.mcp.json` in the repo root points at Alpaca's official MCP server. Any MCP
client (Claude Code, Claude Desktop, Cursor, VS Code) picks it up and can then
inspect the same paper account the agent trades, in plain language.

```bash
export ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=...
claude   # Claude Code reads .mcp.json from the project root
```

Then ask directly: *"what positions am I holding?"*, *"show me the SPY option
chain for next Friday"*, *"what was my last filled order?"*

This is the human-facing lane. The agent never routes an order through MCP —
the deterministic risk gate is the only path to the broker, and a
natural-language interface that could place trades would be a hole straight
through it. MCP is how a person **inspects** the account the agent trades.

## CLI — independent verification

`glassbox/alpaca_cli.py` wraps the Alpaca CLI and uses it as a **second, fully
independent source of truth** about the account.

The trader reads the account through `alpaca-py`. The CLI is a different binary,
a different code path, and its own authentication. Reconciliation already
compares local state against the broker, but both sides of that comparison come
through the same client — if that client is misconfigured, paginating short, or
serving a cached response, the check passes while being wrong. The CLI shares no
plumbing with the trader, so when the two disagree, something real is wrong.

```bash
brew install alpacahq/tap/cli
uv run python -m glassbox.report.run     # includes the cross-check
```

### A measured caveat

Alpaca's CLI is at v0.0.13 and labelled alpha. Measured against this machine,
roughly **one call in six** completes — the rest lose the TLS handshake, with or
without spacing between retries. This is why the cross-check:

- runs at **report time only**, never in the trading path;
- retries six times and then gives up rather than blocking;
- reports **unavailable** when it cannot run, never silent agreement.

A verification that reports success when it did not actually run is worse than
no verification at all. Being explicit about that limitation is more useful than
pretending the integration is solid.
