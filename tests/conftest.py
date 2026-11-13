import pytest

from tests.fake_tasmota import FakePlug


@pytest.fixture
def plug():
    """A running fake plug; stopped after the test."""
    p = FakePlug(power_w=10.0)
    p.start()
    yield p
    p.stop()
