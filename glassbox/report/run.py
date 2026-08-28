"""Nightly report entrypoint. Runs on cron after the US close.

uv run python -m glassbox.report.run
uv run python -m glassbox.report.run --day 2026-09-02 --no-narrative
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from glassbox import alpaca_cli
from glassbox.clock import MARKET_TZ, market_date
from glassbox.config import load_config
from glassbox.report.generate import write_report
from glassbox.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the nightly report")
    parser.add_argument("--day", help="YYYY-MM-DD (default: today, US Eastern)")
    parser.add_argument(
        "--no-narrative",
        action="store_true",
        help="skip the model-written summary; emit numbers only",
    )
    parser.add_argument(
        "--no-cli-check", action="store_true", help="skip the independent Alpaca CLI cross-check"
    )
    args = parser.parse_args()

    cfg = load_config()
    day = (
        datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=MARKET_TZ).date()
        if args.day
        else market_date()
    )

    store = Store(cfg.paths.db)
    llm = None
    if not args.no_narrative:
        try:
            from glassbox.llm import LlmClient

            llm = LlmClient.from_config(cfg)
        except RuntimeError as e:
            print(f"narrative skipped: {e}", file=sys.stderr)

    snapshot = None
    if not args.no_cli_check:
        try:
            from glassbox.data.alpaca_client import trading_client

            check = alpaca_cli.cross_check(trading_client().get_all_positions())
            snapshot = check.as_dict()
            snapshot["agrees"] = check.agrees
            print(f"CLI cross-check: {check.detail}")
        except Exception as e:  # noqa: BLE001 -- a failed cross-check is reported,
            # never allowed to prevent the report being written.
            print(f"CLI cross-check unavailable: {type(e).__name__}: {e}", file=sys.stderr)

    path = write_report(cfg, store, llm, day, snapshot)
    store.close()
    print(f"wrote {path}")
    print(path.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
