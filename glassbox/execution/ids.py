"""Deterministic client order IDs — the idempotency mechanism.

The same (signal, structure, attempt) always produces the same id, so a retry
after a timeout can never create a duplicate position: Alpaca rejects a
repeated client_order_id. Never use a random or timestamped id for an order.
"""

from __future__ import annotations

import hashlib

PREFIX = "gbx"
MAX_LEN = 48  # well under Alpaca's limit, keeps logs readable


def client_order_id(signal_id: str, structure_key: str, attempt: int = 0) -> str:
    """Stable id for an opening order."""
    digest = hashlib.sha256(f"{signal_id}|{structure_key}|{attempt}".encode()).hexdigest()
    return f"{PREFIX}-o-{digest[:32]}"[:MAX_LEN]


def close_order_id(position_id: str, reason: str, attempt: int = 0) -> str:
    """Stable id for a closing order. Reason is part of the key so that a
    time-stop close and a stop-loss close are distinct orders, but a retry of
    either is not."""
    digest = hashlib.sha256(f"{position_id}|{reason}|{attempt}".encode()).hexdigest()
    return f"{PREFIX}-c-{digest[:32]}"[:MAX_LEN]
