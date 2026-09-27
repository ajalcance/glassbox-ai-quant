"""The agent's PreToolUse hooks, run exactly as the harness runs them.

Each case pipes a tool call as JSON into the hook and reads its decision, so
what is tested is the program the harness executes, including how it fails:
a hook that crashes must refuse, because the harness treats a crash as a
non-blocking error and runs the command anyway.

Key- and id-shaped strings are assembled at runtime so that this file never
contains one — the write guard would refuse to save it, and rightly.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_claude_settings import ALPACA_MCP_WRITES

ROOT = Path(__file__).resolve().parents[1]
HOOKS = Path(os.environ.get("GBX_HOOKS_DIR", ROOT / ".claude" / "hooks"))


def _decide(hook: str, payload: dict | str) -> str | None:
    """The hook's refusal reason, or None if it lets the call through."""
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return None
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def _bash(command: str) -> str | None:
    return _decide("guard_bash.py", {"tool_name": "Bash", "tool_input": {"command": command}})


# -- guard_bash ---------------------------------------------------------------

BASH_ALLOWED = [
    "ssh gbx state",
    "ssh gbx checks",
    "ssh gbx deployed",
    "ssh gbx cohort",
    "ssh gbx daily 2026-09-28",
    "ssh gbx tail trader",
    "ssh gbx logs supervisor 30m",
    "ssh gbx deploy trader",
    "ssh gbx state 2>&1",
    "git push origin agent-hooks",
    "git -C . log --oneline -3",
    'git commit -m "never git push --force; never ssh root@host"',
    "uv run pytest -q",
    "uv run python -m glassbox.runner --dry-run",
    "grep -rn soak_infra soak local",
    "git add soak/soak_infra.py",
    "make test",
    "python3 - <<'EOF'\nprint(\"don't\")\nEOF",
]

BASH_REFUSED = [
    # Remote access other than the named host commands.
    "ssh root@64.177.40.87 uptime",
    "ssh -i ~/.ssh/id_ed25519 root@64.177.40.87",
    "ssh -o User=root gbx state",
    "ssh gbx",
    "ssh gbx 'state; docker rm -f glassbox-trader'",
    "ssh gbx state extra",
    "ssh gbx tail caddy",
    "ssh gbx logs trader 30d",
    "ssh gbx daily 2026-9-28",
    "ssh gbx deploy all",
    "ssh gbx state > out.txt",
    "ssh gbx $(echo state)",
    "true && ssh gbx state",
    "ssh gbx state | tail -3",
    "/usr/bin/ssh root@64.177.40.87",
    "nohup ssh root@64.177.40.87 uptime",
    "env FOO=1 ssh root@64.177.40.87",
    "scp local/x.py gbx:/tmp/",
    "rsync -a . gbx:/tmp/",
    "sftp gbx",
    "echo 'x\nssh root@64.177.40.87 uptime",
    "echo ok\nssh root@64.177.40.87 'uptime",  # unparseable line naming a remote
    # History, whatever global options come first.
    "git -C . push --force",
    "git -c core.x=y push origin +main",
    "git push origin :agent-hooks",
    "git push -d origin agent-hooks",
    "git push --force-with-lease",
    "git --no-pager tag v1",
    "git reset --hard HEAD~1",
    "git -C . clean -fd",
    "git commit -n -m x",
    "git -C . config core.hooksPath /dev/null",
    "git reflog expire --all",
    # A second trader or supervisor on the live account.
    "env make run",
    "uv run python -m glassbox.runner",
    "nohup python -m glassbox.supervisor.run",
    "uv run python -m glassbox.drills flatten",
    "python glassbox/runner.py",
    "python local/soak_infra.py all",
]


@pytest.mark.parametrize("command", BASH_ALLOWED)
def test_bash_allows_routine_work(command):
    assert _bash(command) is None


@pytest.mark.parametrize("command", BASH_REFUSED)
def test_bash_refuses(command):
    assert _bash(command) is not None


@pytest.mark.parametrize("hook", ["guard_bash.py", "guard_mcp.py", "guard_write.py"])
def test_a_hook_that_cannot_read_its_input_refuses(hook):
    assert _decide(hook, "not json") is not None
    assert _decide(hook, {"unexpected": True}) is not None


def test_bash_grammar_matches_the_host_dispatcher():
    """The client refuses early; the host is what holds. They must agree, or
    the agent is told a command is fine that the host will refuse — or worse,
    the host starts accepting something the client was built to stop."""

    def load(path: Path):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    hook = load(HOOKS / "guard_bash.py")
    host = load(ROOT / "deploy" / "host" / "gbx_dispatch.py")
    assert hook.GRAMMAR == host.GRAMMAR


# -- guard_mcp ----------------------------------------------------------------


def _mcp(tool: str) -> str | None:
    return _decide("guard_mcp.py", {"tool_name": tool, "tool_input": {}})


@pytest.mark.parametrize(
    "tool", ["get_account_info", "get_all_positions", "get_orders", "get_clock"]
)
def test_mcp_allows_reads(tool):
    assert _mcp(f"mcp__alpaca__{tool}") is None


@pytest.mark.parametrize("tool", ALPACA_MCP_WRITES + ["mcp__alpaca__liquidate_everything"])
def test_mcp_refuses_writes_and_anything_unknown(tool):
    assert _mcp(tool) is not None


# -- guard_write --------------------------------------------------------------

FAKE_ACCOUNT = "PA" + "9Z" * 5
FAKE_KEY_ID = "PK" + "7Q" * 9


def _write(path: Path, text: str, tool: str = "Write") -> str | None:
    if tool == "Write":
        tool_input = {"file_path": str(path), "content": text}
    elif tool == "Edit":
        tool_input = {"file_path": str(path), "old_string": "a", "new_string": text}
    else:
        tool_input = {"file_path": str(path), "edits": [{"old_string": "a", "new_string": text}]}
    return _decide(
        "guard_write.py", {"tool_name": tool, "tool_input": tool_input, "cwd": str(ROOT)}
    )


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_write_refuses_secrets_in_tracked_files(tool):
    readme = ROOT / "README.md"
    assert _write(readme, f"account {FAKE_ACCOUNT}", tool) is not None
    assert _write(readme, f"key {FAKE_KEY_ID}", tool) is not None
    assert _write(readme, "sk-ant-" + "x" * 30, tool) is not None
    assert _write(readme, "-----BEGIN OPENSSH PRIVATE KEY-----", tool) is not None


def test_write_allows_ordinary_text_and_the_contest_account():
    readme = ROOT / "README.md"
    assert _write(readme, "measured on PA31QN3VRL7B, 31 Aug - 25 Sep") is None
    assert _write(readme, "PARAMETERIZE the replay; PKG_CONFIG_PATH_X") is None


def test_write_skips_files_that_never_reach_the_repository(tmp_path):
    assert _write(ROOT / "local" / "notes.md", f"live {FAKE_ACCOUNT}") is None
    assert _write(tmp_path / "memory.md", f"live {FAKE_ACCOUNT}") is None
