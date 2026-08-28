from datetime import date

import pytest

from glassbox.audit import AuditLog
from glassbox.store import Store
from glassbox.structures import Leg, LegSide, Right, Structure, StructureKind

EXP = date(2026, 9, 18)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit")


def leg(symbol, right, strike, side, qty=1):
    return Leg(symbol=symbol, right=right, strike=strike, expiry=EXP, side=side, ratio_qty=qty)


@pytest.fixture
def bull_put():
    """Credit spread: short 440 put covered by long 435 put. Width 5."""
    return Structure(
        kind=StructureKind.BULL_PUT_SPREAD,
        underlying="SPY",
        legs=(
            leg("SPY260918P00440000", Right.PUT, 440, LegSide.SHORT),
            leg("SPY260918P00435000", Right.PUT, 435, LegSide.LONG),
        ),
    )


@pytest.fixture
def naked_put():
    """Uncovered short put — must never be tradable."""
    return Structure(
        kind=StructureKind.BULL_PUT_SPREAD,
        underlying="SPY",
        legs=(leg("SPY260918P00440000", Right.PUT, 440, LegSide.SHORT),),
    )
