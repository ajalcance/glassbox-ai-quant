#!/usr/bin/env python3
"""PreToolUse guard for file writes: nothing secret reaches a file git will track.

This repository is public (invariant 7). A key written into a tracked file is
one `git add` from being published, and a push cannot be taken back. So the
text being written is checked before it lands, if the file is inside the
project and not ignored by git:

  * key-shaped strings (Alpaca, Anthropic, GitHub, Fireworks, private keys);
  * any Alpaca account id except the contest account, which is the only one
    that may appear publicly. The live account id never does.

Ignored paths (local/, .env) and files outside the project, like the agent's
memory, are not checked: they never reach the repository. Any error inside
the hook refuses the write.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

CONTEST_ACCOUNT = "PA31QN3VRL7B"

_HAS_DIGIT = r"(?=[0-9A-Z]*[0-9])"
PATTERNS = {
    "an Alpaca API key id": re.compile(rf"\b(?:PK|AK|CK){_HAS_DIGIT}[0-9A-Z]{{18}}\b"),
    "an Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "a GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "a Fireworks API key": re.compile(r"\bfw_[A-Za-z0-9]{20,}\b"),
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
ACCOUNT_ID = re.compile(rf"\bPA{_HAS_DIGIT}[0-9A-Z]{{10}}\b")


def new_text(tool_input: dict) -> str:
    parts = [tool_input.get("content"), tool_input.get("new_string"), tool_input.get("new_source")]
    parts += [e.get("new_string") for e in tool_input.get("edits") or []]
    return "\n".join(p for p in parts if isinstance(p, str))


def reaches_repository(path: str, project: str) -> bool:
    real, root = os.path.realpath(path), os.path.realpath(project)
    if os.path.commonpath([real, root]) != root:
        return False
    ignored = subprocess.run(
        ["git", "-C", root, "check-ignore", "-q", "--", real], capture_output=True, check=False
    ).returncode
    return ignored != 0  # 1 = not ignored; anything else is treated as tracked


def refusal(tool_input: dict, project: str) -> str | None:
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return "no file path in the request"
    if not reaches_repository(path, project):
        return None
    text = new_text(tool_input)
    for what, pattern in PATTERNS.items():
        if pattern.search(text):
            return f"the text contains {what}; secrets live in .env only (invariant 7)"
    if any(m != CONTEST_ACCOUNT for m in ACCOUNT_ID.findall(text)):
        return "the text contains an account id other than the contest account; this repository is public"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        project = os.environ.get("CLAUDE_PROJECT_DIR") or payload["cwd"]
        reason = refusal(payload["tool_input"], project)
    except Exception as e:  # noqa: BLE001 -- a guard that errors must refuse
        reason = f"guard error, refusing: {type(e).__name__}: {e}"
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"guard_write: {reason}",
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
