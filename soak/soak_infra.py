#!/usr/bin/env python3
"""Infrastructure chaos soak — run ON the server, as root, market CLOSED.

    scp soak/soak_infra.py root@YOUR_SERVER:/root/
    ssh root@YOUR_SERVER 'python3 /root/soak_infra.py all'      # Saturday
    ssh root@YOUR_SERVER 'python3 /root/soak_infra.py reboot'   # then, after
    ssh root@YOUR_SERVER 'python3 /root/soak_infra.py verify'   # ssh back in

Pure stdlib on the host's python3 — the server has no venv; glassbox-level
verification runs INSIDE the trader container via docker exec, so it checks
the same code the stack runs.

Scenarios:
  kill-containers   SIGKILL each glassbox container; restart policy must revive
                    it, audit chains must verify afterwards
  docker-restart    restart the docker daemon under the running stack
  partition [secs]  DROP all outbound 443 (Alpaca, Fireworks — everything) for
                    a while; supervisor ticks must CONTINUE (timeouts working),
                    and the system must recover cleanly when the net returns
  reboot            reboot the host (run `verify` after sshing back in)
  verify            full invariant sweep: containers, chains, supervisor
                    freshness, disk, NTP
  all               kill-containers + docker-restart + partition + verify
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime

CONTAINERS = ("glassbox-trader", "glassbox-supervisor", "glassbox-scheduler",
              "glassbox-dashboard", "glassbox-caddy")
FINDINGS = "/root/soak-infra-findings.jsonl"
PARTITION_DEFAULT = 180
PARTITION_FAILSAFE_EXTRA = 120  # belt-and-braces rule removal even if we die

results: list[dict] = []


def sh(*cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True,
                          timeout=timeout, check=False)


def report(ok: bool, scenario: str, check: str, detail: str) -> None:
    row = {"ts": datetime.now(UTC).isoformat(), "ok": ok,
           "scenario": scenario, "check": check, "detail": detail}
    results.append(row)
    with open(FINDINGS, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"  [{'ok  ' if ok else 'FAIL'}] {check}: {detail}", flush=True)


def running(name: str) -> bool:
    out = sh("docker", "inspect", "-f", "{{.State.Running}}", name)
    return out.stdout.strip() == "true"


def restart_count(name: str) -> int:
    out = sh("docker", "inspect", "-f", "{{.RestartCount}}", name)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


def wait_until(pred, timeout: float, interval: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def chains_verify(scenario: str) -> None:
    out = sh("docker", "exec", "glassbox-trader", "python", "-c",
             "from glassbox.audit import verify_day; ok,n,b=verify_day('audit'); "
             "print(ok, n, b)")
    line = out.stdout.strip()
    report(line.startswith("True"), scenario, "audit_chains",
           f"verify_day inside container -> {line or out.stderr.strip()[:200]}")


def supervisor_log_age() -> float | None:
    out = sh("docker", "logs", "--tail", "1", "--timestamps", "glassbox-supervisor")
    lines = (out.stdout.strip() or out.stderr.strip()).splitlines()
    if not lines:
        return None
    try:
        ts = datetime.fromisoformat(lines[-1].split(" ", 1)[0])
        return (datetime.now(UTC) - ts).total_seconds()
    except ValueError:
        return None


# --------------------------------------------------------------------------

def scenario_kill_containers() -> None:
    print("\n=== kill-containers ===", flush=True)
    for name in CONTAINERS:
        if not running(name):
            report(True, "kill", f"{name}.skipped", "not running (not part of tonight's stack)")
            continue
        before = restart_count(name)
        # Killing the container's main process from the HOST namespace is the
        # only faithful crash simulation, and it took two wrong attempts to get
        # here. `docker kill` is an operator stop through the API, which
        # restart policies deliberately do not undo. `docker exec ... kill -9 1`
        # does nothing at all: the kernel ignores SIGKILL aimed at PID 1 from
        # inside its own PID namespace. The host-side PID has no such
        # protection, so this is a genuine unexpected death — exactly what the
        # restart policy exists to recover from.
        host_pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", name).stdout.strip()
        if not host_pid.isdigit() or host_pid == "0":
            report(False, "kill", f"{name}.pid", f"could not resolve host pid ({host_pid!r})")
            continue
        sh("kill", "-9", host_pid)
        # The name flips running->restarting->running fast; the restart COUNT
        # incrementing is the unambiguous proof the policy actually fired.
        revived = wait_until(
            lambda n=name, b=before: restart_count(n) > b and running(n), timeout=90
        )
        report(revived, "kill", f"{name}.revived",
               f"restart policy revived it (restarts {before} -> {restart_count(name)})"
               if revived else "NOT running 90s after in-container SIGKILL of PID 1")
        if revived:
            time.sleep(5)
            still = running(name)
            report(still, "kill", f"{name}.stable",
                   "stable 5s after revival" if still else "crash-looping after revival")
    chains_verify("kill")


def scenario_docker_restart() -> None:
    print("\n=== docker-restart ===", flush=True)
    sh("systemctl", "restart", "docker", timeout=180)
    wait_until(lambda: all(running(n) for n in CONTAINERS if restart_count(n) >= 0),
               timeout=180)
    states = {n: running(n) for n in CONTAINERS}
    report(all(states.values()), "docker-restart", "stack_returned", str(states))
    chains_verify("docker-restart")


def scenario_partition(seconds: int) -> None:
    print(f"\n=== partition ({seconds}s, all outbound 443) ===", flush=True)
    rule = ["OUTPUT", "-p", "tcp", "--dport", "443", "-j", "DROP"]
    # Failsafe: an independent process removes the rule even if this one dies.
    subprocess.Popen(
        ["nohup", "bash", "-c",
         f"sleep {seconds + PARTITION_FAILSAFE_EXTRA}; iptables -D {' '.join(rule)} 2>/dev/null"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sh("iptables", "-I", *rule)
    try:
        age_start = supervisor_log_age()
        time.sleep(seconds / 2)
        mid_age = supervisor_log_age()
        # The whole point of the timeout fix: calls fail fast instead of
        # hanging, so the supervisor KEEPS TICKING through a dead network.
        report(mid_age is not None and mid_age < 60, "partition", "supervisor_alive_during",
               f"supervisor log age mid-partition: {mid_age}s (start {age_start}s) — "
               + ("still ticking through the outage" if mid_age is not None and mid_age < 60
                  else "FROZEN: a broker call is hanging"))
        time.sleep(seconds / 2)
    finally:
        sh("iptables", "-D", *rule)
    print("  network restored, watching recovery…", flush=True)
    recovered = wait_until(lambda: (a := supervisor_log_age()) is not None and a < 30,
                           timeout=120)
    report(recovered, "partition", "recovered",
           "supervisor ticking normally after restore" if recovered
           else "supervisor still silent 120s after network restore")
    time.sleep(10)
    chains_verify("partition")


def scenario_reboot() -> None:
    print("\n=== reboot === (run `verify` after sshing back in)", flush=True)
    report(True, "reboot", "initiated", "rebooting now")
    sh("systemctl", "reboot")


def scenario_verify() -> None:
    print("\n=== verify ===", flush=True)
    up = sh("uptime", "-p").stdout.strip()
    report(True, "verify", "uptime", up)
    states = {n: running(n) for n in CONTAINERS}
    report(all(states.values()), "verify", "containers", str(states))
    age = supervisor_log_age()
    report(age is not None and age < 60, "verify", "supervisor_fresh",
           f"last log line {age}s ago" if age is not None else "no supervisor log")
    chains_verify("verify")
    df = sh("df", "-P", "/").stdout.splitlines()
    pct = int(df[1].split()[4].rstrip("%")) if len(df) > 1 else -1
    report(0 <= pct < 80, "verify", "disk", f"root disk {pct}% used")
    ntp = sh("timedatectl", "show", "-p", "NTPSynchronized", "--value").stdout.strip()
    report(ntp == "yes", "verify", "ntp", f"NTPSynchronized={ntp}")
    leftover = sh("iptables", "-C", "OUTPUT", "-p", "tcp", "--dport", "443", "-j", "DROP")
    report(leftover.returncode != 0, "verify", "no_leftover_partition",
           "no partition rule lingering" if leftover.returncode != 0
           else "PARTITION RULE STILL ACTIVE — remove it now")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else PARTITION_DEFAULT
    print(f"INFRA SOAK {cmd} — {datetime.now(UTC).isoformat()}", flush=True)

    if cmd in ("kill-containers", "all"):
        scenario_kill_containers()
    if cmd in ("docker-restart", "all"):
        scenario_docker_restart()
    if cmd in ("partition", "all"):
        scenario_partition(secs)
    if cmd in ("verify", "all"):
        scenario_verify()
    if cmd == "reboot":
        scenario_reboot()
        return 0

    failed = [r for r in results if not r["ok"]]
    print(f"\n{'FAILED' if failed else 'PASSED'} — {len(results) - len(failed)}/{len(results)} "
          f"checks ok; findings in {FINDINGS}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
