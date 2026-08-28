"""Verify the Alpaca MCP server actually starts and exposes tools.

    uv run python -m glassbox.verify_mcp

Speaks the MCP handshake over stdio directly rather than trusting that a config
file is correct. A `.mcp.json` that has never been exercised is a claim, not an
integration — and this is the requirement the submission's eligibility rests on,
so it gets checked rather than assumed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from glassbox.config import load_config

PROTOCOL = "2026-06-18"
STARTUP_TIMEOUT = 180  # first run downloads the package via uvx


def _env() -> dict:
    env = dict(os.environ)
    if "ALPACA_API_KEY_ID" in env:
        env.setdefault("ALPACA_API_KEY", env["ALPACA_API_KEY_ID"])
    if "ALPACA_API_SECRET_KEY" in env:
        env.setdefault("ALPACA_SECRET_KEY", env["ALPACA_API_SECRET_KEY"])
    env.setdefault("ALPACA_PAPER_TRADE", "True")
    return env


def _request(proc, payload: dict) -> None:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _read_response(proc, want_id: int, deadline_lines: int = 200) -> dict | None:
    """Read until the response with the expected id arrives. Servers commonly
    emit log lines on stdout, so non-JSON is skipped rather than fatal."""
    for _ in range(deadline_lines):
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == want_id:
            return message
    return None


def main() -> int:
    cfg = load_config()
    command = ["uvx", "alpaca-mcp-server"]
    print(f"Starting MCP server: {' '.join(command)}")
    print("(first run downloads the package; this can take a minute)")

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_env(),
        )
    except FileNotFoundError:
        print("FAIL  uvx not found — install uv first")
        return 1

    try:
        _request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "glassbox-verify", "version": "1.0"},
                },
            },
        )
        init = _read_response(proc, 1)
        if not init or "result" not in init:
            print(f"FAIL  no initialize response: {init}")
            print(proc.stderr.read()[:600] if proc.stderr else "")
            return 1

        info = init["result"].get("serverInfo", {})
        print(f"  ok    connected to {info.get('name', '?')} {info.get('version', '')}")

        _request(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _request(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listing = _read_response(proc, 2)
        if not listing or "result" not in listing:
            print(f"FAIL  no tools/list response: {listing}")
            return 1

        tools = listing["result"].get("tools", [])
        print(f"  ok    {len(tools)} tools exposed")
        for tool in sorted(tools, key=lambda t: t["name"])[:20]:
            print(f"          {tool['name']}")
        if len(tools) > 20:
            print(f"          ... and {len(tools) - 20} more")

        names = {t["name"] for t in tools}
        # The tools a judge would actually reach for when inspecting the account.
        expected_shape = any("account" in n for n in names) and any("position" in n for n in names)
        if not expected_shape:
            print("FAIL  server does not expose account/position tools")
            return 1

        print("\nMCP VERIFY PASSED")
        print(f"  config: .mcp.json  (analyst model {cfg.llm.analyst_model.split('/')[-1]})")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
