#!/usr/bin/env python3
"""PreToolUse guard for the agent's shell commands.

Permission rules compare text from the first character, so they cannot see
`git -C . push --force`, `env make run`, or an ssh target hidden behind an
option. This hook splits the command into words and checks what would
actually run:

  * Remote access. `ssh` is the one command that leaves the OS sandbox, so it
    is allowed only as `ssh gbx <verb> <args>`, standing alone, with the same
    grammar the host's dispatcher enforces (deploy/host/gbx_dispatch.py; a test
    fails if the two drift). scp, sftp and rsync are refused outright.
  * Git history: force, mirror, delete and `+` refspec pushes, tags, hard
    resets, clean, skipped hooks — whatever global options come first.
  * A second trader or supervisor on the live account, and the chaos soak
    pointed anywhere but the dev lane.

Anything it cannot read is refused if it could be one of the above, and any
error inside the hook refuses the command: a guard that fails open is not one.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

SERVICE = r"(trader|supervisor|scheduler|dashboard)"
SINCE = r"[1-9][0-9]{0,2}[smh]"
DATE = r"20[0-9]{2}-[01][0-9]-[0-3][0-9]"

GRAMMAR: dict[str, tuple[str, ...]] = {
    "state": (),
    "checks": (),
    "deployed": (),
    "cohort": (),
    "daily": (DATE,),
    "tail": (SERVICE,),
    "logs": (SERVICE, SINCE),
    "deploy": (SERVICE,),
}
HOST_ALIAS = "gbx"

REMOTE = {"ssh", "scp", "sftp", "rsync"}
WRAPPERS = {"command", "exec", "nohup", "env", "time", "nice", "timeout", "caffeinate", "stdbuf"}
SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", ")"}
READ_ONLY = {
    "grep",
    "rg",
    "cat",
    "head",
    "tail",
    "less",
    "wc",
    "ls",
    "git",
    "sed",
    "awk",
    "diff",
    "find",
}

GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
PUSH_REFUSED = {
    "--force",
    "-f",
    "--mirror",
    "--delete",
    "-d",
    "--tags",
    "--prune",
    "--force-if-includes",
}
LIVE_MAKE_TARGETS = {
    "run",
    "supervisor",
    "drill-trip",
    "drill-flat",
    "drill-clean",
    "kill",
    "resume",
    "up",
    "down",
}
LIVE_MODULES = {"glassbox.supervisor.run", "glassbox.supervisor"}
LIVE_DRILLS = {"round_trip", "flatten", "cleanup"}

_REMOTE_WORD = re.compile(r"(^|[\s;&|(`$])(ssh|scp|sftp|rsync)(\s|$)")
_TRAILING_STDERR = re.compile(r"\s+2>&1\s*$")


class Refuse(Exception):
    pass


def _lex(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _segments(tokens: list[str]) -> list[list[str]]:
    out, current = [], []
    for tok in tokens:
        if tok in SEPARATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def _unwrap(words: list[str]) -> list[str]:
    """Drop leading VAR=value assignments and wrappers like `env`, `nohup`,
    `timeout 60`, so the command they run is what gets checked."""
    words = list(words)
    while words:
        head = words[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", head):
            words.pop(0)
        elif os.path.basename(head) in WRAPPERS:
            words.pop(0)
            while words and (
                words[0].startswith("-") or re.fullmatch(r"\d+[smhd]?|[A-Za-z_]\w*=.*", words[0])
            ):
                words.pop(0)
        else:
            break
    if words:
        words[0] = os.path.basename(words[0])
    return words


def _check_ssh(words: list[str], alone: bool) -> None:
    usage = (
        "production is reached only as `ssh gbx <verb> <args>`, standing alone; verbs: "
        + ", ".join(sorted(GRAMMAR))
    )
    if words[0] != "ssh":
        raise Refuse(f"{words[0]} is not allowed; {usage}")
    if not alone:
        raise Refuse(f"ssh chained with other commands; {usage}")
    if len(words) < 3 or words[1] != HOST_ALIAS:
        raise Refuse(usage)
    verb, args = words[2], words[3:]
    pattern = GRAMMAR.get(verb)
    if pattern is None:
        raise Refuse(f"unknown verb {verb!r}; {usage}")
    if len(args) != len(pattern) or not all(re.fullmatch(p, a) for p, a in zip(pattern, args)):
        raise Refuse(f"usage: ssh gbx {verb} " + " ".join(f"<{p}>" for p in pattern))


def _check_git(words: list[str]) -> None:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in GIT_GLOBAL_WITH_VALUE else 1
    if i >= len(words):
        return
    sub, args = words[i], words[i + 1 :]
    if sub == "push":
        for a in args:
            if a in PUSH_REFUSED or a.startswith(("--force", "+", ":")):
                raise Refuse(
                    f"git push {a} rewrites or deletes remote history; that is the owner's act"
                )
    elif sub == "tag":
        raise Refuse("tags are the owner's act")
    elif sub == "reset" and "--hard" in args:
        raise Refuse("git reset --hard discards work; not without the owner")
    elif sub == "clean":
        raise Refuse("git clean deletes untracked files; not without the owner")
    elif sub in {"filter-branch", "filter-repo", "update-ref", "replace"}:
        raise Refuse(f"git {sub} rewrites history; that is the owner's act")
    elif sub == "reflog" and args[:1] in (["expire"], ["delete"]):
        raise Refuse("the reflog is the recovery path; not without the owner")
    elif sub == "config" and any("hookspath" in a.lower() for a in args):
        raise Refuse("git hooks configuration is the owner's")
    if "--no-verify" in args or (sub == "commit" and "-n" in args):
        raise Refuse("skipping git hooks is not allowed")


def _check_live(words: list[str]) -> None:
    head = words[0]
    if head == "make" and any(w in LIVE_MAKE_TARGETS for w in words[1:]):
        raise Refuse(
            "that make target trades, flattens or runs the stack; production runs on the host"
        )
    for i, w in enumerate(words[:-1]):
        if w == "-m" and words[i + 1] == "glassbox.runner" and "--dry-run" not in words:
            raise Refuse(
                "a live trader belongs on the host; never two on one account (use --dry-run)"
            )
        if w == "-m" and words[i + 1] in LIVE_MODULES:
            raise Refuse("a second supervisor could flatten the live account; it runs on the host")
        if w == "-m" and words[i + 1] == "glassbox.drills" and any(d in words for d in LIVE_DRILLS):
            raise Refuse("that drill trades or flattens; drills run on the dev lane")
    if any(w.endswith("glassbox/runner.py") for w in words) and "--dry-run" not in words:
        raise Refuse("a live trader belongs on the host; never two on one account")
    if head not in READ_ONLY and any(re.search(r"soak_infra(\.py)?$", w) for w in words):
        raise Refuse("the infrastructure soak never runs from here, and never against production")


def check(command: str) -> None:
    command = _TRAILING_STDERR.sub("", command)
    try:
        segments = _segments(_lex(command.replace("\n", " ; ")))
    except ValueError:
        # Unbalanced quotes, usually prose in a heredoc. Check line by line, and
        # refuse a line that cannot be read if it could reach a remote host.
        segments = []
        for line in command.splitlines():
            try:
                segments += _segments(_lex(line))
            except ValueError:
                if _REMOTE_WORD.search(line):
                    raise Refuse("could not parse a line that mentions remote access") from None
    alone = len(segments) == 1
    for seg in segments:
        words = _unwrap(seg)
        if not words:
            continue
        if words[0] in REMOTE:
            _check_ssh(words, alone)
        elif words[0] == "git":
            _check_git(words)
        _check_live(words)


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"guard_bash: {reason}",
                }
            }
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        check(payload["tool_input"]["command"])
    except Refuse as e:
        _deny(str(e))
    except Exception as e:  # noqa: BLE001 -- a guard that errors must refuse
        _deny(f"guard error, refusing: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
