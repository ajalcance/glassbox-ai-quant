"""Alpaca CLI integration — an independent second source of truth.

The trader reads the account through alpaca-py. This module reads the *same*
account through Alpaca's CLI: a different binary, a different code path, its own
authentication. That makes it genuinely independent rather than a second call to
the same client.

Reconciliation already compares local state against the broker. Adding a source
that shares no code with the trader turns that into a real cross-check: if our
SDK client is misconfigured, silently paginating short, or reading a cached
response, the CLI disagrees and we halt. A verification path that shares its
plumbing with the thing it verifies is not verification.

The CLI is also the natural surface for cron and shell operations, which is what
it was built for. Every call is read-only — no order ever goes through here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

BINARY = "alpaca"
TIMEOUT = 30
ATTEMPTS = 6  # see the note in run(): v0.0.13 is unreliable in practice


class CliUnavailableError(Exception):
    """The CLI is not installed or not answering. Callers treat verification as
    unavailable — never as agreement."""


def is_available() -> bool:
    return shutil.which(BINARY) is not None


def _env() -> dict:
    """The CLI reads ALPACA_API_KEY / ALPACA_SECRET_KEY; our own variables are
    named for the SDK, so they are mapped here rather than duplicated in .env."""
    env = dict(os.environ)
    if "ALPACA_API_KEY_ID" in env:
        env.setdefault("ALPACA_API_KEY", env["ALPACA_API_KEY_ID"])
    if "ALPACA_API_SECRET_KEY" in env:
        env.setdefault("ALPACA_SECRET_KEY", env["ALPACA_API_SECRET_KEY"])
    env.setdefault("ALPACA_PAPER", "true")
    return env


def run(*args: str) -> object:
    """Run a read-only CLI command and return parsed JSON."""
    if not is_available():
        raise CliUnavailableError(
            "alpaca CLI not on PATH — install with `brew install alpacahq/tap/cli`"
        )
    # Measured on v0.0.13 (alpha): roughly one call in six completes; the rest
    # lose the TLS handshake, with or without spacing between attempts. That
    # measurement is the reason this is a best-effort, report-time cross-check
    # and never sits in the trading path. A dependency this unreliable must not
    # be able to delay or block an order, and an unavailable cross-check is
    # reported as unavailable rather than quietly counted as agreement.
    last = "no attempt made"
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(0.5 * attempt)
        try:
            proc = subprocess.run(
                [BINARY, *args],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env=_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            last = "timed out"
            continue
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout or "null")
            except json.JSONDecodeError:
                last = "returned non-JSON"
                continue
        last = f"exited {proc.returncode}: {proc.stderr.strip()[:160]}"
    raise CliUnavailableError(f"alpaca {' '.join(args)} failed after {ATTEMPTS}: {last}")


def account() -> dict:
    return run("account", "get") or {}


def positions() -> list:
    result = run("position", "list")
    return result if isinstance(result, list) else []


def clock() -> dict:
    return run("clock") or {}


@dataclass(frozen=True, slots=True)
class CrossCheck:
    """Agreement between the SDK's view and the CLI's view of the same account."""

    available: bool
    agrees: bool
    equity: float | None = None
    position_count: int | None = None
    sdk_position_count: int | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "agrees": self.agrees,
            "equity": self.equity,
            "position_count": self.position_count,
            "sdk_position_count": self.sdk_position_count,
            "detail": self.detail,
        }


def cross_check(sdk_positions: list) -> CrossCheck:
    """Compare the CLI's account view against the SDK's.

    An unavailable CLI reports `agrees=False, available=False` — never silent
    agreement. A verification that passes when it did not run is worse than none.
    """
    if not is_available():
        return CrossCheck(False, False, detail="alpaca CLI not installed")
    try:
        acct = account()
        cli_positions = positions()
    except CliUnavailableError as e:
        return CrossCheck(False, False, detail=str(e))

    sdk_count = len(sdk_positions)
    cli_count = len(cli_positions)
    agrees = cli_count == sdk_count
    return CrossCheck(
        available=True,
        agrees=agrees,
        equity=float(acct.get("equity", 0) or 0) or None,
        position_count=cli_count,
        sdk_position_count=sdk_count,
        detail=(
            "SDK and CLI agree"
            if agrees
            else f"CLI sees {cli_count} positions, SDK sees {sdk_count}"
        ),
    )
