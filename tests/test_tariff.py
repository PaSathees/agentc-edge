import pytest

from agentc.calc import Band, Schedule, month_lkr, saving_lkr

# Made-up round rates with Sri Lanka's shape: going over 60 units moves every unit to table 2
TARIFF = (
    Schedule(60, (Band(30, 10, fixed=100), Band(60, 20, fixed=200))),
    Schedule(None, (Band(60, 30), Band(90, 40, fixed=400), Band(None, 50, fixed=500))),
)


@pytest.mark.parametrize(
    ("kwh", "lkr"),
    [
        (0, 100),  # nothing used still pays the first fixed charge
        (20, 20 * 10 + 100),
        (30, 30 * 10 + 100),  # a band edge belongs to the lower band
        (50, 30 * 10 + 20 * 20 + 200),
        (60, 30 * 10 + 30 * 20 + 200),
        (70, 60 * 30 + 10 * 40 + 400),  # over 60: table 2 for every unit
        (100, 60 * 30 + 30 * 40 + 10 * 50 + 500),
    ],
)
def test_month_bill_by_hand(kwh, lkr):
    assert month_lkr(kwh, TARIFF) == pytest.approx(lkr)


def test_saving_that_drops_back_under_60_is_worth_the_table_switch():
    # 70 -> 58 units: 2600 -> 1060, far more than 12 units at any one rate
    assert saving_lkr(70, 12, TARIFF) == pytest.approx(2600 - 1060)


def test_saving_high_in_the_month_is_priced_at_the_top_band():
    assert saving_lkr(100, 5, TARIFF) == pytest.approx(5 * 50)


@pytest.mark.parametrize(
    "bad",
    [
        (Schedule(60, (Band(None, 10),)),),  # last schedule has a limit: 100 units fits none
        (Schedule(None, (Band(60, 30),)),),  # last band has a limit: units past 60 unpriced
    ],
)
def test_a_tariff_that_cannot_price_every_month_is_refused(bad):
    with pytest.raises(ValueError, match="no limit"):
        month_lkr(100, bad)
