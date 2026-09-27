import json

import pytest

from glassbox.audit import AuditLog
from glassbox.oprecord import parse_fields, write


def test_writes_a_chained_record_under_its_own_role(tmp_path):
    first = write(
        str(tmp_path), "deploy", "deploy_start", {"service": "trader", "commit": "abc1234"}
    )
    second = write(str(tmp_path), "deploy", "deploy_done", {"service": "trader", "running": "true"})

    files = list(tmp_path.glob("*-deploy.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["deploy_start", "deploy_done"]
    assert rows[0]["service"] == "trader" and rows[0]["commit"] == "abc1234"
    assert AuditLog.verify_chain(files[0]) == (True, 2)
    assert second["prev_hash"] != first["prev_hash"]


@pytest.mark.parametrize("role", ["trader", "supervisor", "repair", "../trader"])
def test_refuses_roles_it_does_not_own(tmp_path, role):
    """Sharing a live writer's day file forks its hash chain (12 Sep)."""
    with pytest.raises(ValueError):
        write(str(tmp_path), role, "deploy_start", {})
    assert not list(tmp_path.iterdir())


def test_fields_are_key_value_and_cannot_forge_chain_fields():
    assert parse_fields(["service=trader", "note=a=b"]) == {"service": "trader", "note": "a=b"}
    for bad in (["noequals"], ["=x"], ["prev_hash=0"], ["kind=trade"], ["ts=now"]):
        with pytest.raises(ValueError):
            parse_fields(bad)
