import pytest

from tests.fake_tasmota import FakePlug


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Tests never reach the real model, even when run with the key in the environment."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def plug():
    """A running fake plug; stopped after the test."""
    p = FakePlug(power_w=10.0)
    p.start()
    yield p
    p.stop()


@pytest.fixture
def quiet_jobs(monkeypatch):
    """The slow job passes (watch, look, learn, change check) do nothing: for tests of when
    another job runs, not of what these do."""
    from agentc import jobs  # noqa: PLC0415

    for name in ("watch_pass", "look_pass", "learn_pass", "change_check", "timer_check"):
        monkeypatch.setattr(jobs, name, lambda *a, **k: [])  # noqa: ARG005
