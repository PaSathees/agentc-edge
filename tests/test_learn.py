import pytest

from agentc.learn import Levels, levels, values


def test_a_desk_has_off_standby_and_active():
    assert levels([0.0] * 50 + [3.0] * 30 + [27.0] * 20) == Levels(standby=3.0, active=27.0)


def test_levels_are_split_on_a_log_scale():
    # on a plain scale, 2 W and 20 W would be grouped; on a log scale 20 W and 40 W are
    assert levels([2.0] * 10 + [20.0] * 10 + [40.0] * 10) == Levels(standby=2.0, active=30.0)


@pytest.mark.parametrize(
    "watts",
    [
        [0.0] * 100,  # always off
        [0.3, 0.5] * 50,  # only meter noise
        [5.0] * 100,  # one level: the printer's 5 W
        [3.0] * 97 + [27.0] * 3,  # a high group under 5% of the time on
        [3.0] * 50 + [5.0] * 50,  # active under 2 x standby
        [0.0] * 99 + [3.0],  # one reading on
        [0.4] * 50 + [27.0] * 50,  # meter noise is off, not standby: one level
    ],
)
def test_no_two_clear_levels_means_nothing_learned(watts):
    assert levels(watts) is None


def test_use_and_unusual_sit_half_way_and_standby_left_on_counts():
    got = values(Levels(standby=3.0, active=27.0))
    assert {k: v for k, (v, _) in got.items()} == {"use_w": 12.0, "more_w": 12.0, "small_w": 1.5}
    assert got["use_w"][1] == "half way from standby to active (standby 3 W, active 27 W)"


def test_small_w_is_at_least_1_w():
    assert values(Levels(standby=1.0, active=13.0))["small_w"][0] == 1.0
