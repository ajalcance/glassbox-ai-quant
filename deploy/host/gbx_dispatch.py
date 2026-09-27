#!/usr/bin/env python3
"""The only thing the coding agent can run on the production host.

Installed as /usr/local/sbin/gbx-dispatch and reached through the `gbxagent`
user, whose one authorized key carries a forced command (see
install-agent-access.sh). Whatever the client asks for arrives here as a
single string, SSH_ORIGINAL_COMMAND, and is either one of the verbs below with
arguments that match exactly, or refused. Nothing is passed to a shell.

The agent watches the session while the owner sleeps, so it needs to read
state, logs and reports, and to deploy what the owner has merged. Everything
else on this host — clearing a halt, flattening, the ledger volumes, the chaos
lane — stays with the owner, who reaches the host with their own key.

The client-side hook (.claude/hooks/guard_bash.py) carries the same grammar,
and a test fails if the two drift. This copy is the one that holds if the
client is ever wrong.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

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

REPO = "/home/glassbox/glassbox-ai-quant"
GBX_STATE = "/usr/local/bin/gbx-state"
DAILY = "/root/daily.py"
COHORT = "/root/cohort.py"
STATE_URL = "http://127.0.0.1:8899/state.json"
INVOCATION_LOG = "/var/log/gbx-agent.log"

ET = ZoneInfo("America/New_York")
# The trader holds `_deferred` in memory, so a restart inside the opening
# auction loses the entries waiting on it (measured 15 Sep).
AUCTION_WINDOW = (dtime(9, 25), dtime(9, 45))
SETTLE_SECONDS = 15

# One line per news story the trader looked at and passed on. The tail is for
# events a person should hear about, not for the analyst's reading list.
_SKIP_LINE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] {3}- {3}")
_CLOCK = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ")


class Refused(Exception):
    """The request is not one this host will run."""


def parse(original: str) -> tuple[str, list[str]]:
    try:
        tokens = shlex.split(original)
    except ValueError as e:
        raise Refused(f"unparseable command: {e}") from None
    if not tokens:
        raise Refused("no command; this key runs only: " + ", ".join(sorted(GRAMMAR)))
    verb, args = tokens[0], tokens[1:]
    if verb not in GRAMMAR:
        raise Refused(f"unknown verb {verb!r}; allowed: " + ", ".join(sorted(GRAMMAR)))
    pattern = GRAMMAR[verb]
    if len(args) != len(pattern) or not all(re.fullmatch(p, a) for p, a in zip(pattern, args)):
        raise Refused(f"usage: {verb} " + " ".join(f"<{p}>" for p in pattern))
    return verb, args


def in_auction_window(now: datetime) -> bool:
    local = now.astimezone(ET)
    return local.weekday() < 5 and AUCTION_WINDOW[0] <= local.time() < AUCTION_WINDOW[1]


class EventFilter:
    """Drops the per-story skip lines and collapses a line repeated tick after
    tick (a standing HALTED prints every 15 seconds) into one note."""

    def __init__(self) -> None:
        self._last: str | None = None
        self._repeats = 0

    def feed(self, line: str) -> list[str]:
        if _SKIP_LINE.match(line):
            return []
        key = _CLOCK.sub("", line)
        if key == self._last:
            self._repeats += 1
            return []
        out = []
        if self._repeats:
            out.append(f"  (previous line repeated {self._repeats} more time(s))")
        out.append(line)
        self._last, self._repeats = key, 0
        return out


# -- execution (host only) ------------------------------------------------------


def _as_glassbox(*argv: str) -> list[str]:
    return ["sudo", "-u", "glassbox", *argv]


def _git(*args: str) -> str:
    out = subprocess.run(
        _as_glassbox("git", "-C", REPO, *args), capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        _as_glassbox("docker", "compose", *args),
        cwd=REPO,
        capture_output=capture,
        text=True,
        check=False,
    )


def _stream(argv: list[str]) -> int:
    filt = EventFilter()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            for line in filt.feed(raw.rstrip("\n")):
                print(line, flush=True)
    finally:
        proc.terminate()
    return proc.wait()


def _record(kind: str, **fields: str) -> bool:
    argv = ["run", "--rm", "--no-deps", "-T", "scheduler", "python", "-m", "glassbox.oprecord"]
    argv += ["--role", "deploy", "--kind", kind, *(f"{k}={v}" for k, v in fields.items())]
    result = _compose(*argv, capture=True)
    print(f"  audit: {(result.stdout or result.stderr).strip().splitlines()[-1:]}")
    return result.returncode == 0


def run_deploy(service: str) -> int:
    if in_auction_window(datetime.now(ET)):
        raise Refused("deploy refused 09:25-09:45 ET: the opening auction holds deferred entries")
    if _git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise Refused("the host checkout is not on main")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise Refused("the host checkout has local changes")
    before = _git("rev-parse", "--short", "HEAD")
    _git("fetch", "-q", "origin", "main")
    try:
        _git("merge", "--ff-only", "-q", "origin/main")
    except subprocess.CalledProcessError:
        raise Refused("origin/main does not fast-forward from the host checkout") from None
    after = _git("rev-parse", "--short", "HEAD")
    print(f"main {before} -> {after}")

    build = _compose("build", service, capture=True)
    print("\n".join((build.stdout + build.stderr).strip().splitlines()[-3:]))
    if build.returncode != 0:
        raise Refused(f"build failed for {service}; nothing restarted")
    if not _record("deploy_start", service=service, previous=before, commit=after):
        raise Refused("could not write the deploy audit record; nothing restarted")

    if _compose("up", "-d", "--no-deps", service).returncode != 0:
        _record("deploy_failed", service=service, commit=after)
        raise Refused(f"compose up failed for {service}")
    time.sleep(SETTLE_SECONDS)
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}} {{.RestartCount}}", f"glassbox-{service}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    running, _, restarts = state.partition(" ")
    _record("deploy_done", service=service, commit=after, running=running, restarts=restarts)
    print(f"{service} at {after}: running={running} restarts={restarts}")
    return 0 if running == "true" else 1


def run_checks() -> int:
    with urllib.request.urlopen(STATE_URL, timeout=10) as r:
        state = json.load(r)
    checks = state["checks"]
    failing = [k for k, v in checks.items() if not v["ok"]]
    print(f"cycle {state['cycle']} | {len(checks)} checks | failing: {failing or 'none'}")
    for name, check in checks.items():
        print(f"  {'ok  ' if check['ok'] else 'FAIL'} {name}: {check['detail']}")
    return 0


def execute(verb: str, args: list[str]) -> int:
    if verb == "state":
        return subprocess.run([GBX_STATE], check=False).returncode
    if verb == "checks":
        return run_checks()
    if verb == "deployed":
        print(_git("log", "--oneline", "-1"))
        fmt = "{{.Names}} {{.Status}} {{.Image}}"
        return subprocess.run(
            ["docker", "ps", "--filter", "name=glassbox-", "--format", fmt], check=False
        ).returncode
    if verb == "daily":
        return subprocess.run(["python3", DAILY, args[0]], check=False).returncode
    if verb == "cohort":
        return subprocess.run(["python3", COHORT], check=False).returncode
    if verb == "tail":
        return _stream(["docker", "logs", "-f", "--since", "2m", f"glassbox-{args[0]}"])
    if verb == "logs":
        return _stream(["docker", "logs", "--since", args[1], f"glassbox-{args[0]}"])
    if verb == "deploy":
        return run_deploy(args[0])
    raise Refused(f"no handler for {verb!r}")


def _log(original: str, outcome: str) -> None:
    try:
        with open(INVOCATION_LOG, "a") as f:
            f.write(f"{datetime.now(ET).isoformat()} {outcome} {original!r}\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    original = argv[1] if len(argv) == 2 else ""
    try:
        verb, args = parse(original)
        _log(original, "run")
        return execute(verb, args)
    except Refused as e:
        _log(original, "refused")
        print(f"gbx: {e}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, BrokenPipeError):  # the client hung up on a tail
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
