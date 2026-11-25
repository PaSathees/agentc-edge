"""scripts/dev.py: the local copy points every plug at a fake, or does not start."""

import importlib.util
import re
from pathlib import Path

import pytest

from agentc import config
from tests.test_cli import EXAMPLE

spec = importlib.util.spec_from_file_location(
    "dev", Path(__file__).parent.parent / "scripts/dev.py"
)
assert spec and spec.loader
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)

FAKES = {"Plug-6": "127.0.0.1:9001", "Plug-7": "127.0.0.1:9002"}


def source(tmp_path, plugs):
    """The example config with its [plugs] table written as given."""
    text = EXAMPLE.read_text()
    start = re.search(r"^\[plugs\]", text, flags=re.M).start()  # pyright: ignore[reportOptionalMemberAccess]
    end = text.index("\n[", start + 1)
    path = tmp_path / "config.toml"
    path.write_text(text[:start] + "[plugs]\n" + plugs + text[end:])
    return path


@pytest.mark.parametrize("plugs", ['"Plug-6" = "192.0.2.6"\nPlug-7 = "192.0.2.7"\n',
                                   'Plug-6 = "192.0.2.6"\n  "Plug-7"="192.0.2.7"\n'])  # fmt: skip
def test_every_plug_points_at_its_fake_however_its_key_is_written(tmp_path, plugs):
    src = source(tmp_path, plugs)
    assert dev.plug_names(src) == ["Plug-6", "Plug-7"]
    out = dev.dev_config(src, FAKES, [], tmp_path / "dev" / "config.toml")
    assert config.load(out).plugs == FAKES


def test_a_plug_left_on_its_real_address_stops_the_start(tmp_path):
    src = source(tmp_path, '"Plug-6" = "192.0.2.6"\n"Plug-7" = "192.0.2.7"\n')
    out = tmp_path / "dev" / "config.toml"
    with pytest.raises(SystemExit, match="Plug-7 would point at a real plug"):
        dev.dev_config(src, {"Plug-6": "127.0.0.1:9001"}, [], out)
    assert not out.exists()


def test_write_sets_access_once_even_in_the_last_section(tmp_path):
    src = source(tmp_path, '"Plug-6" = "192.0.2.6"\n"Plug-7" = "192.0.2.7"\n')
    src.write_text(
        src.read_text().rstrip("\n") + '\n[devices."Plug-9"]\nname = "Box"\naccess = "none"'
    )
    out = dev.dev_config(src, FAKES | {"Plug-9": "127.0.0.1:9003"}, ["Plug-9", "Plug-7"],
                         tmp_path / "dev" / "config.toml")  # fmt: skip
    text = out.read_text()
    assert text.count('[devices."Plug-9"]\naccess = "write"') == 1
    assert config.device(config.load(out), "Plug-7").access == "write"  # was none
