from pathlib import Path

import pytest

from agentc import config
from agentc.calc import Band, Factor, Period, Schedule, TimeOfUse

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"

TOU = """
[site]
name = "home"
tz = "Asia/Colombo"
month_kwh = 200

[carbon]
id = "grid-2025"
factor = 0.5
source = "Example Energy Balance 2025"
valid_from = "2025-01-01"

[plugs]
"Plug-2" = "192.0.2.12"

[tariff]
kind = "time_of_use"
source = "Example"
fixed = 500.0
periods = [{ start = "05:30", rate = 30.0 }, { start = "18:30", rate = 60.0 }]
"""


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_the_example_config_loads():  # keeps the committed example valid
    c = config.load(EXAMPLE)
    assert (c.site, c.tz) == ("home", "Asia/Colombo")
    assert c.plugs == {"Plug-1": "192.0.2.11", "Plug-2": "192.0.2.12"}
    assert c.tariff_source == "Example tariff, not real rates"
    assert isinstance(c.tariff, tuple)  # block: a tuple of schedules
    assert c.tariff[0] == Schedule(60, (Band(30, 10.0, 100.0), Band(60, 20.0, 200.0)))
    assert c.tariff[1].bands[-1] == Band(None, 50.0, 500.0)  # no up_to = no limit
    assert c.levy == 0.025
    assert c.month_kwh == 150
    assert c.factor.value == 0.4242


def test_paths_are_relative_to_the_config_file():
    c = config.load(EXAMPLE)
    assert c.app_db == EXAMPLE.parent / "app.db"
    assert c.energy_db == EXAMPLE.parent / "energy.db"


def test_time_of_use_tariff(tmp_path):
    c = config.load(write(tmp_path, TOU))
    assert c.tariff == TimeOfUse((Period("05:30", 30.0), Period("18:30", 60.0)), fixed=500.0)
    assert c.app_db == tmp_path / "app.db"  # [paths] left out: defaults beside the file


def test_house_units_and_grid_factor(tmp_path):
    c = config.load(write(tmp_path, TOU))
    assert c.month_kwh == 200
    assert c.factor == Factor("grid-2025", 0.5, "Example Energy Balance 2025", "2025-01-01")
    assert c.levy == 0.0  # no levy given: none


def test_unknown_tariff_kind_is_refused(tmp_path):
    with pytest.raises(ValueError, match="block"):
        config.load(write(tmp_path, TOU.replace('"time_of_use"', '"flat"')))


def test_a_block_table_that_cannot_price_every_month_fails_at_load(tmp_path):
    text = EXAMPLE.read_text().replace("    { rate = 50.0, fixed = 500.0 },\n", "")
    with pytest.raises(ValueError, match="no limit"):
        config.load(write(tmp_path, text))


def test_unknown_time_zone_fails_at_load(tmp_path):
    with pytest.raises(ValueError, match="time zone"):
        config.load(write(tmp_path, TOU.replace("Asia/Colombo", "Asia/Nowhere")))
