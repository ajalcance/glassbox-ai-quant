import re
from datetime import date

import pytest

from glassbox.structures import (
    Leg,
    LegSide,
    Right,
    Structure,
    StructureKind,
    UndefinedRiskError,
    assert_defined_risk,
    max_loss_per_spread,
    structure_key,
)
from tests.conftest import leg


def test_defined_risk_passes_for_vertical(bull_put):
    assert_defined_risk(bull_put)  # must not raise


def test_naked_short_rejected(naked_put):
    with pytest.raises(UndefinedRiskError, match="single leg"):
        assert_defined_risk(naked_put)


def test_more_shorts_than_longs_rejected():
    ratioed = Structure(
        kind=StructureKind.BULL_PUT_SPREAD,
        underlying="SPY",
        legs=(
            leg("SPY260918P00440000", Right.PUT, 440, LegSide.SHORT, qty=2),
            leg("SPY260918P00435000", Right.PUT, 435, LegSide.LONG, qty=1),
        ),
    )
    with pytest.raises(UndefinedRiskError, match="uncovered short leg"):
        assert_defined_risk(ratioed)


def test_calendar_rejected():
    calendar = Structure(
        kind=StructureKind.BULL_PUT_SPREAD,
        underlying="SPY",
        legs=(
            leg("SPY260918P00440000", Right.PUT, 440, LegSide.SHORT),
            Leg("SPY261016P00435000", Right.PUT, 435, date(2026, 10, 16), LegSide.LONG),
        ),
    )
    with pytest.raises(UndefinedRiskError, match="mixed expiries"):
        assert_defined_risk(calendar)


def test_short_call_not_covered_by_long_put():
    """A long put does NOT cover a short call — different right."""
    bogus = Structure(
        kind=StructureKind.IRON_CONDOR,
        underlying="SPY",
        legs=(
            leg("SPY260918C00460000", Right.CALL, 460, LegSide.SHORT),
            leg("SPY260918P00435000", Right.PUT, 435, LegSide.LONG),
        ),
    )
    with pytest.raises(UndefinedRiskError, match="short Right.CALL|short call"):
        assert_defined_risk(bogus)


def test_credit_max_loss_is_width_minus_credit(bull_put):
    # width 5.00, credit 1.20 -> risk 3.80 per share -> $380 per spread
    assert max_loss_per_spread(bull_put, net_price=-1.20) == pytest.approx(380.0)


def test_debit_max_loss_is_the_debit():
    debit = Structure(
        kind=StructureKind.CALL_DEBIT_SPREAD,
        underlying="SPY",
        legs=(
            leg("SPY260918C00450000", Right.CALL, 450, LegSide.LONG),
            leg("SPY260918C00455000", Right.CALL, 455, LegSide.SHORT),
        ),
    )
    assert max_loss_per_spread(debit, net_price=2.10) == pytest.approx(210.0)


def test_max_loss_refuses_undefined_risk(naked_put):
    with pytest.raises(UndefinedRiskError):
        max_loss_per_spread(naked_put, net_price=-1.0)


def test_structure_key_is_stable_and_order_independent(bull_put):
    reversed_legs = Structure(
        kind=bull_put.kind, underlying=bull_put.underlying, legs=tuple(reversed(bull_put.legs))
    )
    assert structure_key(bull_put) == structure_key(reversed_legs)
    assert re.match(r"SPY\|bull_put_spread\|20260918\|", structure_key(bull_put))


def test_credit_at_or_above_width_rejected(bull_put):
    """A credit >= width implies risk-free arbitrage. Accepting it would compute
    max_loss = 0, making the heat check pass trivially and sizing unbounded."""
    from glassbox.structures import ImplausiblePricingError

    with pytest.raises(ImplausiblePricingError, match="risk-free arbitrage"):
        max_loss_per_spread(bull_put, net_price=-5.00)  # width is exactly 5
    with pytest.raises(ImplausiblePricingError):
        max_loss_per_spread(bull_put, net_price=-7.50)


def test_free_debit_rejected():
    from glassbox.structures import ImplausiblePricingError

    debit = Structure(
        kind=StructureKind.CALL_DEBIT_SPREAD,
        underlying="SPY",
        legs=(
            leg("SPY260918C00450000", Right.CALL, 450, LegSide.LONG),
            leg("SPY260918C00455000", Right.CALL, 455, LegSide.SHORT),
        ),
    )
    with pytest.raises(ImplausiblePricingError, match="free long position"):
        max_loss_per_spread(debit, net_price=0.0)


def test_max_loss_is_always_positive_for_valid_pricing(bull_put):
    for credit in (0.10, 1.20, 4.99):
        assert max_loss_per_spread(bull_put, net_price=-credit) > 0
