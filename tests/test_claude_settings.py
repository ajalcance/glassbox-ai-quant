"""The coding agent's permission model, pinned so it cannot drift.

The agent is meant to run without confirmation prompts: a prompt is only a
control while someone is watching, and the US session runs while the owner
sleeps. So every action either runs or is refused outright, and the refusals
live in `.claude/settings.json`. These tests are what stops that file quietly
loosening.

The command cases model Claude Code's rule matching: `*` matches anything,
and a rule is compared against the whole command from its first character.
That is why a flag rule needs a wildcard on each side — `Bash(--admin)` never
matches `gh pr merge 5 --admin`. The model is ours; the live probes after
each change are what confirm the harness agrees with it.
"""

import json
import re
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / ".claude" / "settings.json"

# Every Alpaca MCP tool that changes the account. The agent inspects the broker
# through MCP and never trades through it (invariants 1 and 3).
ALPACA_MCP_WRITES = [
    "mcp__alpaca__place_option_order",
    "mcp__alpaca__place_stock_order",
    "mcp__alpaca__place_crypto_order",
    "mcp__alpaca__replace_order_by_id",
    "mcp__alpaca__cancel_order_by_id",
    "mcp__alpaca__cancel_all_orders",
    "mcp__alpaca__close_position",
    "mcp__alpaca__close_all_positions",
    "mcp__alpaca__exercise_options_position",
    "mcp__alpaca__do_not_exercise_options_position",
    "mcp__alpaca__update_account_config",
    "mcp__alpaca__create_locate",
    "mcp__alpaca__create_watchlist",
    "mcp__alpaca__update_watchlist_by_id",
    "mcp__alpaca__delete_watchlist_by_id",
    "mcp__alpaca__add_asset_to_watchlist_by_id",
    "mcp__alpaca__remove_asset_from_watchlist_by_id",
]

# Files that define the agent's own authority. An agent must never widen it.
SELF_PROTECTED = [
    "/.claude/settings.json",
    "/.claude/settings.local.json",
    "/.claude/hooks/**",
    "/.mcp.json",
]
EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]

# Bare names match at any depth. `.env.example` stays readable on purpose.
SECRET_FILES = [".env", ".env.local", ".env.development", ".env.staging", ".env.production"]

MUST_BE_DENIED = [
    # Owner-only: releases, merges, labels, repository settings.
    "git tag",
    "git tag v1.0",
    "git push --tags",
    "git push origin --tags",
    "gh release create v1.0",
    "gh pr merge 5 --squash",
    "gh pr edit 5 --add-label guard-change-approved",
    "gh pr edit 5 --remove-label guard-change-approved",
    "gh pr create --title t --label guard-change-approved",
    "gh label create guard-change-approved",
    "gh repo edit --visibility private",
    "gh secret set ALPACA_API_KEY",
    "gh variable set X",
    "gh ruleset list",
    "gh workflow run ci.yml",
    "gh auth token",
    "gh gist create glassbox/gate.py",
    "gh api -X PUT repos/ajalcance/glassbox-ai-quant/pulls/5/merge",
    "gh api repos/ajalcance/glassbox-ai-quant/branches/main/protection --method DELETE",
    "gh api repos/ajalcance/glassbox-ai-quant/issues/5/labels -f labels[]=x",
    "gh api repos/ajalcance/glassbox-ai-quant/issues/5/labels",
    "gh api graphql --input query.json",
    # Irreversible history.
    "git push --force",
    "git push origin main --force",
    "git push --force-with-lease origin main",
    "git push -f origin main",
    "git push origin main -f",
    "git push origin +main",
    "git push origin --delete agent-permissions",
    "git push --mirror",
    "git reset --hard HEAD~1",
    "git reset HEAD~1 --hard",
    "git clean -fdx",
    "git commit --no-verify -m x",
    "git config core.hooksPath /dev/null",
    # A second trader or supervisor against a live account (standing rule).
    "make run",
    "make supervisor",
    "make drill-trip",
    "make drill-flat",
    "make drill-clean",
    "make kill",
    "make resume",
    "make up",
    "make down",
    "uv run python -m glassbox.supervisor.run",
    "uv run python -m glassbox.drills round_trip",
    "uv run python -m glassbox.drills flatten",
    "uv run python -m glassbox.drills cleanup",
    # Outside the repository's own code.
    "scp local/x.py root@64.177.40.87:/root/",
    "rsync -a . root@64.177.40.87:/tmp/",
    "sudo rm -rf /",
    "docker compose up -d",
    "npm publish",
    "uv publish",
]

MUST_STAY_ALLOWED = [
    "uv run pytest -q",
    "uv run ruff check --fix .",
    "make test",
    "make check",
    "make drill-sim",
    "git status",
    "git log --oneline -5",
    "git add tests/test_claude_settings.py",
    "git commit -F /tmp/msg.txt",
    "git push origin agent-permissions",
    "git fetch origin",
    "git switch -c agent-permissions",
    "gh api repos/ajalcance/glassbox-ai-quant/branches/main",
    "gh pr create --title t --body-file body.md",
    "gh pr view 5",
    "gh pr checks 5",
    "grep -rn force glassbox",
    "rm -rf local/tmp",
]


def _settings() -> dict:
    return json.loads(SETTINGS.read_text())


def _rules(settings: dict, kind: str) -> list[str]:
    return settings.get("permissions", {}).get(kind, [])


def _bash_patterns(rules: list[str]) -> list[str]:
    return [r[len("Bash(") : -1] for r in rules if r.startswith("Bash(") and r.endswith(")")]


def _matches(pattern: str, command: str) -> bool:
    regex = "".join(".*" if c == "*" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, command, re.DOTALL) is not None


def _denied(settings: dict, command: str) -> bool:
    return any(_matches(p, command) for p in _bash_patterns(_rules(settings, "deny")))


def test_no_ask_rules():
    """`ask` answers itself in the desktop harness and interrupts when it
    doesn't. Each action is allowed or denied; there is no third answer."""
    assert not _rules(_settings(), "ask")


def test_alpaca_mcp_cannot_change_the_account():
    settings = _settings()
    deny = set(_rules(settings, "deny"))
    missing = [t for t in ALPACA_MCP_WRITES if t not in deny]
    assert not missing, f"Alpaca MCP write tools not denied: {missing}"
    allowed = [r for r in _rules(settings, "allow") if r.startswith("mcp__alpaca")]
    assert not allowed, f"Alpaca MCP tools must not be blanket-allowed: {allowed}"


def test_agent_cannot_edit_its_own_authority():
    deny = set(_rules(_settings(), "deny"))
    missing = [f"{t}({p})" for p in SELF_PROTECTED for t in EDIT_TOOLS if f"{t}({p})" not in deny]
    assert not missing, f"self-protection gaps: {missing}"


def test_secret_files_denied_at_any_depth():
    deny = set(_rules(_settings(), "deny"))
    missing = [
        f"{t}({name})"
        for name in SECRET_FILES
        for t in ("Read", "Edit", "Write")
        if f"{t}({name})" not in deny
    ]
    assert not missing, f"secret files reachable: {missing}"


def test_dangerous_commands_are_denied():
    settings = _settings()
    leaks = [c for c in MUST_BE_DENIED if not _denied(settings, c)]
    assert not leaks, f"not denied: {leaks}"


def test_routine_commands_are_not_denied():
    settings = _settings()
    blocked = [c for c in MUST_STAY_ALLOWED if _denied(settings, c)]
    assert not blocked, f"routine work denied: {blocked}"


def test_no_rule_starts_with_a_flag():
    """A rule is compared from the start of the command, and no command starts
    with a flag — so `Bash(--admin)` would silently match nothing."""
    settings = _settings()
    dead = [p for p in _bash_patterns(_rules(settings, "deny")) if p.startswith("-")]
    assert not dead, f"rules that can never match: {dead}"


def test_ssh_is_never_pre_approved():
    """Production is reached through the guarded host commands, not through
    remembered one-off approvals of arbitrary remote shell."""
    allowed = _bash_patterns(_rules(_settings(), "allow"))
    ssh = [p for p in allowed if p.split(" ", 1)[0] in {"ssh", "scp", "rsync"}]
    assert not ssh, f"pre-approved remote access: {ssh}"


def test_blanket_bash_only_behind_a_bash_hook():
    """Allowing every command is safe only while a PreToolUse hook inspects
    them. Without one, the deny list is the only thing standing."""
    settings = _settings()
    if "Bash" not in _rules(settings, "allow"):
        return
    matchers = [h.get("matcher") for h in settings.get("hooks", {}).get("PreToolUse", [])]
    assert "Bash" in matchers, "blanket Bash allow with no Bash hook"


def test_matcher_models_the_harness():
    """The cases above are only as good as this model of the matching."""
    assert _matches("git push *--force*", "git push origin main --force")
    assert _matches("git tag*", "git tag")
    assert not _matches("--admin", "gh pr merge 5 --admin")
    assert not _matches("make run*", "make drill-sim")
