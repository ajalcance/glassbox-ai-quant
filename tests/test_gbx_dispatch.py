"""The production host's command gate (deploy/host/gbx_dispatch.py).

It runs as root on the host behind the agent's forced-command key, so the
parts that decide — the grammar, the deploy window, the tail filter — are
pure and tested here. The parts that act are thin calls to git, docker and
the host's report scripts, verified live after install.
"""

import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "host" / "gbx_dispatch.py"
_spec = importlib.util.spec_from_file_location("gbx_dispatch", _PATH)
gbx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gbx)

ET = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    "original, expected",
    [
        ("state", ("state", [])),
        ("daily 2026-09-28", ("daily", ["2026-09-28"])),
        ("logs trader 45m", ("logs", ["trader", "45m"])),
        ("deploy supervisor", ("deploy", ["supervisor"])),
        ("  tail   trader  ", ("tail", ["trader"])),
    ],
)
def test_parse_accepts_the_grammar(original, expected):
    assert gbx.parse(original) == expected


@pytest.mark.parametrize(
    "original",
    [
        "",
        "bash",
        "state; rm -rf /",
        "state && id",
        "state extra",
        "tail caddy",
        "tail trader supervisor",
        "logs trader 0m",
        "logs trader 1d",
        "daily 26-09-28",
        "daily 2026-09-28; id",
        "deploy",
        "deploy all",
        "deploy 'trader; id'",
        "deploy trader\nid",
        "state '",
    ],
)
def test_parse_refuses_everything_else(original):
    with pytest.raises(gbx.Refused):
        gbx.parse(original)


def test_main_refuses_without_running_anything(capsys):
    assert gbx.main(["gbx-dispatch", "docker rm -f glassbox-trader"]) == 2
    assert "unknown verb" in capsys.readouterr().err
    assert gbx.main(["gbx-dispatch"]) == 2


@pytest.mark.parametrize(
    "when, blocked",
    [
        (datetime(2026, 9, 28, 9, 24, 59, tzinfo=ET), False),
        (datetime(2026, 9, 28, 9, 25, tzinfo=ET), True),
        (datetime(2026, 9, 28, 9, 44, 59, tzinfo=ET), True),
        (datetime(2026, 9, 28, 9, 45, tzinfo=ET), False),
        (datetime(2026, 9, 27, 9, 30, tzinfo=ET), False),  # Sunday
        # 13:30 UTC is 09:30 ET: the window is Eastern whatever clock asks.
        (datetime(2026, 9, 28, 13, 30, tzinfo=ZoneInfo("UTC")), True),
        (datetime(2026, 9, 28, 21, 30, tzinfo=ZoneInfo("Asia/Manila")), True),
    ],
)
def test_deploy_window_is_the_opening_auction_in_eastern(when, blocked):
    assert gbx.in_auction_window(when) is blocked


def test_tail_drops_skip_lines_and_collapses_repeats():
    f = gbx.EventFilter()
    lines = [
        "[13:31:02]   -   AAPL: edge ratio 0.8 below 1.2",
        "[13:31:05] TRADE TLT: bear call 16 Oct x1",
        "[13:31:20] HALTED — heartbeat stale",
        "[13:31:35] HALTED — heartbeat stale",
        "[13:31:50] HALTED — heartbeat stale",
        "[13:32:05] CLOSE TLT trail",
    ]
    out = [o for line in lines for o in f.feed(line)]
    assert out == [
        "[13:31:05] TRADE TLT: bear call 16 Oct x1",
        "[13:31:20] HALTED — heartbeat stale",
        "  (previous line repeated 2 more time(s))",
        "[13:32:05] CLOSE TLT trail",
    ]
