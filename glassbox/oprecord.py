"""Append one out-of-process record to the audit log, under its own role.

Deploys and operator actions happen outside the trader, so they are written by
this CLI rather than by the processes they affect. Like `repair.py`, it owns its
role's day file exclusively: sharing the trader's file while the trader is live
forks the hash chain (12 Sep), so the roles a person or a host script may write
under are listed here and `trader` is never one of them.

    python -m glassbox.oprecord --role deploy --kind deploy_start service=trader commit=abc123
"""

from __future__ import annotations

ROLES = ("deploy", "operator")


def parse_fields(pairs: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"expected key=value, got {pair!r}")
        if key in {"record_id", "ts", "prev_hash", "kind"}:
            raise ValueError(f"{key!r} is set by the audit log, not the caller")
        fields[key] = value
    return fields


def write(audit_dir: str, role: str, kind: str, fields: dict[str, str]) -> dict:
    from glassbox.audit import AuditLog

    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    return AuditLog(audit_dir, role=role).append(kind, fields)


def _cli() -> int:
    import argparse

    from glassbox.config import load_config

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--role", required=True, choices=ROLES)
    ap.add_argument("--kind", required=True)
    ap.add_argument("fields", nargs="*", metavar="key=value")
    args = ap.parse_args()
    record = write(load_config().paths.audit_dir, args.role, args.kind, parse_fields(args.fields))
    print(f"{args.role} {record['kind']} {record['record_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
