"""A timer's days, as Tasmota keeps them (7 chars, Sunday first) and in words."""

import pytest

from agentc.devices.base import day_bits, days_words


@pytest.mark.parametrize(
    ("days", "bits"),
    [("weekdays", "0111110"), ("every_day", "1111111"), ("1111100", "1111100"),
     ("0000000", None), ("111110", None), ("11111x0", None), ("someday", None)],
)  # fmt: skip
def test_days_are_a_set_name_or_7_chars(days, bits):
    assert day_bits(days) == bits


@pytest.mark.parametrize(
    ("bits", "words"),
    [("1111100", "Sunday to Thursday"), ("1100011", "Friday to Monday"),
     ("0111110", "Monday to Friday"), ("1000001", "weekends"), ("1111111", "every day"),
     ("0101000", "Monday and Wednesday"), ("0000000", "no days"), ("0000010", "Friday"),
     ("weekdays", "Monday to Friday"), ("sometimes", "sometimes")],
)  # fmt: skip
def test_days_in_words_runs_go_round_the_weeks_end(bits, words):
    assert days_words(bits) == words


def test_a_change_keeps_one_spelling_for_its_days():
    from agentc.planner import Change  # noqa: PLC0415

    bits = Change("add_pair", off="23:00", on="07:00", days="0111110")
    assert bits == Change("add_pair", off="23:00", on="07:00", days="weekdays")
    assert bits.to_json() == Change("add_pair", off="23:00", on="07:00", days="weekdays").to_json()
    assert Change("add_pair", off="23:00", on="07:00", days="1111100").days == "1111100"
