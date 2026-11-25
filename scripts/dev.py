"""See the web app on your own computer before you deploy it.

    uv run python scripts/dev.py --from-hub edge-hub   # copy the hub's data first
    uv run python scripts/dev.py                       # use the copy you have

It copies the hub's app.db and energy.db into dev/ (git ignores it), starts one fake plug
for every plug in config.toml, and runs the web app at http://127.0.0.1:8770. The copy
points every plug at its fake, so nothing here can reach a real plug. There is no API key,
so no model is called. Sign in with the password "dev". Ctrl+C stops it all.

--write Plug-9 sets a device to write in the copy, to try the Devices page switch.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # for tests.fake_tasmota

from tests.fake_tasmota import FakePlug  # noqa: E402

DEV = ROOT / "dev"
PORT = 8770
SNAP = (
    "import sqlite3, sys; s = sqlite3.connect(sys.argv[1]);"
    " d = sqlite3.connect(sys.argv[2]); s.backup(d)"
)


def copy_from_hub(host: str, data: str) -> None:
    """A consistent copy of the hub's databases, made with SQLite's backup while it runs."""
    (DEV / "data").mkdir(parents=True, exist_ok=True)
    for name in ("app.db", "energy.db"):
        snap = f"{data}/dev-copy-{name}"
        inside = f"python -c {shlex.quote(SNAP)} /data/{name} /data/dev-copy-{name}"
        run(["ssh", host, f"cd ~/agentc-edge && docker compose exec -T jobs {inside}"])
        run(["scp", "-q", f"{host}:{snap}", str(DEV / "data" / name)])
        run(["ssh", host, f"rm -f {shlex.quote(snap)}"])
        print(f"Copied {name} from {host}.")


def dev_config(fakes: dict[str, str], write: list[str]) -> Path:
    """config.toml with every plug pointed at its fake, and --write devices set to write."""
    text = (ROOT / "config.toml").read_text()
    for plug, address in fakes.items():
        text = re.sub(rf'^("{re.escape(plug)}"\s*=\s*)"[^"]*"', rf'\1"{address}"', text, flags=re.M)
    for plug in write:
        section = f'[devices."{plug}"]'
        if section not in text:
            text += f'\n{section}\nname = "{plug}"\n'
        text = re.sub(rf"^(\[devices\.\"{re.escape(plug)}\"\]\n(?:(?!\[).*\n)*?)access = .*\n",
                      r"\1", text, flags=re.M)  # fmt: skip
        text = text.replace(section, f'{section}\naccess = "write"', 1)
    path = DEV / "config.toml"
    path.write_text(text)
    return path


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> None:
    ask = argparse.ArgumentParser(description="See the web app on your own computer.")
    ask.add_argument("--from-hub", metavar="HOST", help="copy the data from this SSH host first")
    ask.add_argument("--hub-data", default="/data", help="the data folder on the hub")
    ask.add_argument("--write", action="append", default=[], metavar="PLUG",
                     help="set this plug's device to write in the copy")  # fmt: skip
    args = ask.parse_args()
    if args.from_hub:
        copy_from_hub(args.from_hub, args.hub_data)
    if not (DEV / "data" / "app.db").exists():
        sys.exit("No data yet. Run with --from-hub edge-hub first.")
    plugs = re.findall(r'^"(Plug-[^"]+)"\s*=', (ROOT / "config.toml").read_text(), flags=re.M)
    fakes = {}
    for n, plug in enumerate(plugs):
        fake = FakePlug(power_w=5.0, mac=f"DE:VE:00:00:00:{n:02X}")
        fake.relay_on = True
        fake.start()
        fakes[plug] = fake.ip
    env = {**os.environ, "AGENTC_CONFIG": str(dev_config(fakes, args.write)),
           "AGENTC_DATA": str(DEV / "data"), "AGENTC_SECRET": "dev-only",
           "AGENTC_PASSWORD": "dev", "AGENTC_HUB": ""}  # fmt: skip
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("AGENTC_TAILSCALE_USER", None)
    print(f"Fake plugs: {len(fakes)}. Open http://127.0.0.1:{PORT}/setup (password: dev)")
    try:
        cmd = [sys.executable, "-m", "uvicorn", "agentc.app:app", "--port", str(PORT),
               "--reload", "--reload-dir", str(ROOT / "src")]  # fmt: skip
        subprocess.run(cmd, env=env, check=False)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
