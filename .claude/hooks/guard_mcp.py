#!/usr/bin/env python3
"""PreToolUse guard for the Alpaca MCP server: read tools only, by name.

The agent inspects the broker through MCP and never trades through it
(invariants 1 and 3). settings.json denies every write tool the server has
today, but `uvx alpaca-mcp-server` is not pinned, so a release that adds a tool
would add it unguarded. This hook inverts that: a tool runs only if it is on
the list below, so anything new is refused until someone reads it and adds it.
"""

from __future__ import annotations

import json
import sys

PREFIX = "mcp__alpaca__"

READ_TOOLS = frozenset(
    {
        "fetch_alpaca_doc",
        "get_account_activities",
        "get_account_activities_by_type",
        "get_account_config",
        "get_account_info",
        "get_all_assets",
        "get_all_positions",
        "get_alpaca_endpoint_docs",
        "get_asset",
        "get_calendar",
        "get_clock",
        "get_corporate_action_announcement",
        "get_corporate_action_announcements",
        "get_corporate_actions",
        "get_crypto_bars",
        "get_crypto_latest_bar",
        "get_crypto_latest_orderbook",
        "get_crypto_latest_quote",
        "get_crypto_latest_trade",
        "get_crypto_quotes",
        "get_crypto_snapshot",
        "get_crypto_trades",
        "get_fixed_income_latest_quotes",
        "get_locate",
        "get_locate_quotes",
        "get_locates",
        "get_market_movers",
        "get_most_active_stocks",
        "get_news",
        "get_open_position",
        "get_option_bars",
        "get_option_chain",
        "get_option_contract",
        "get_option_contracts",
        "get_option_exchange_codes",
        "get_option_latest_quote",
        "get_option_latest_trade",
        "get_option_snapshot",
        "get_option_trades",
        "get_order_by_client_id",
        "get_order_by_id",
        "get_orders",
        "get_portfolio_history",
        "get_stock_bars",
        "get_stock_latest_bar",
        "get_stock_latest_quote",
        "get_stock_latest_trade",
        "get_stock_quotes",
        "get_stock_snapshot",
        "get_stock_trades",
        "get_watchlist_by_id",
        "get_watchlists",
        "list_alpaca_api_endpoints",
        "search_alpaca_api_specs",
        "search_alpaca_docs",
    }
)


def refusal(tool_name: str) -> str | None:
    name = tool_name[len(PREFIX) :] if tool_name.startswith(PREFIX) else None
    if name in READ_TOOLS:
        return None
    return f"guard_mcp: {tool_name} is not a known read-only Alpaca tool; the agent never trades through MCP"


def main() -> int:
    try:
        reason = refusal(json.load(sys.stdin)["tool_name"])
    except Exception as e:  # noqa: BLE001 -- a guard that errors must refuse
        reason = f"guard_mcp: guard error, refusing: {type(e).__name__}: {e}"
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
