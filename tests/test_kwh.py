from datetime import UTC, datetime, timedelta

import pytest

from agentc.calc import kwh

T0 = datetime(2026, 11, 1, 19, 30, tzinfo=UTC)


def meter(*totals):
    """The counter as store.counter returns it: one reading a minute from T0."""
    return [(T0 + timedelta(minutes=i), t) for i, t in enumerate(totals)]


def test_kwh_is_how_far_the_counter_rose():
    assert kwh(meter(1.00, 1.10, 1.25)) == pytest.approx(0.25)


def test_a_reboot_drop_is_skipped_not_subtracted():
    # Tasmota saves its counter now and then; after a power cut it restarts from the last save
    assert kwh(meter(1.00, 1.20, 1.10, 1.30)) == pytest.approx(0.40)  # 0.20 before + 0.20 after


@pytest.mark.parametrize("counter", [meter(), meter(1.5)])
def test_under_two_readings_is_zero(counter):
    assert kwh(counter) == 0.0
