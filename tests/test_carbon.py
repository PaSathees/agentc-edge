import pytest

from agentc.calc import Factor, kgco2e

# A made-up factor; the real one goes in config.toml [carbon], with its source
NEW = Factor("grid-2025", 0.40, "Example Energy Balance 2025, p. 12", "2026-01-01")


def test_kgco2e_is_kwh_times_the_factor():
    assert kgco2e(10, NEW) == pytest.approx(4.0)
