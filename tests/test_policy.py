from dataclasses import fields

import pytest

from agentc.policy import DEFAULT, NAMES, Policy, explain, override


def test_every_value_says_why():
    assert all(f.metadata["why"] for f in fields(Policy))


def test_the_defaults():  # left_on_min was 180 before Phase 3b
    assert DEFAULT == Policy(
        factor=2.0, more_w=10.0, run_min=30, small_w=3.0, left_on_min=30, rarely_on=0.2,
        on_w=2.0, min_days=7, use_w=10.0, use_min=20, step_min=30, margin_min=30,
        max_off_h=12, history_days=14, same_within_min=30, expires_h=24, drift_h=3,
        override_min=30,
    )  # fmt: skip


def test_an_override_changes_only_what_it_names():
    p = override(DEFAULT, {"max_off_h": 16, "more_w": 20}, where="[policy]")
    assert (p.max_off_h, p.more_w, p.factor) == (16, 20, 2.0)


def test_an_unknown_name_fails_and_lists_the_known_ones():
    with pytest.raises(ValueError, match=r"\[policy\]: unknown policy max_of_h. Known: factor, "):
        override(DEFAULT, {"max_of_h": 16}, where="[policy]")


@pytest.mark.parametrize("value", ["12", True])
def test_a_value_that_is_not_a_number_fails(value):
    with pytest.raises(ValueError, match="max_off_h must be a number"):
        override(DEFAULT, {"max_off_h": value}, where="[policy]")


def test_explain_says_where_each_value_came_from_and_why():
    p = override(DEFAULT, {"max_off_h": 16, "more_w": 20, "small_w": 1.5}, where="test")
    rows = explain(p, site=frozenset({"max_off_h"}), device=frozenset({"more_w"}),
                   learned={"small_w": "learned: half the standby"})  # fmt: skip
    got = {name: (value, source, why) for name, value, source, why in rows}
    assert got["more_w"][:2] == (20, "device")
    assert got["max_off_h"][:2] == (16, "site")
    assert got["small_w"] == (1.5, "learned", "learned: half the standby")
    assert got["factor"][1] == "default"
    assert list(got) == list(NAMES)
