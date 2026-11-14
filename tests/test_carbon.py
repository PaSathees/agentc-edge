from datetime import date

import pytest

from agentc import store
from agentc.calc import Factor, kgco2e

# Made-up values; the real one comes from the SLSEA Energy Balance (owner to do)
OLD = Factor("grid-2024", 0.50, "Example Energy Balance 2024, p. 10", "2025-01-01")
NEW = Factor("grid-2025", 0.40, "Example Energy Balance 2025, p. 12", "2026-01-01")


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    db.execute("INSERT INTO sites (id, name, tz) VALUES (1, 'home', 'Asia/Colombo')")
    return db


def test_kgco2e_is_kwh_times_the_factor():
    assert kgco2e(10, NEW) == pytest.approx(4.0)


def test_the_factor_in_force_on_a_date(db):
    store.save_grid_factor(db, site_id=1, factor=OLD)
    store.save_grid_factor(db, site_id=1, factor=NEW)
    assert store.grid_factor(db, site_id=1, on=date(2025, 11, 30)) == OLD  # last year's months
    assert store.grid_factor(db, site_id=1, on=date(2026, 1, 1)) == NEW  # from its first day
    assert store.grid_factor(db, site_id=1, on=date(2026, 11, 14)) == NEW


def test_no_factor_yet_is_none(db):
    store.save_grid_factor(db, site_id=1, factor=NEW)
    assert store.grid_factor(db, site_id=1, on=date(2025, 12, 31)) is None
