"""See the web app on your own computer before you deploy it.

    uv run python scripts/dev.py --from-hub edge-hub   # copy the hub's data first
    uv run python scripts/dev.py                       # use the copy you have

It copies the hub's app.db and energy.db into dev/ (git ignores it), starts one fake plug
for every plug in config.toml, and runs the web app at http://127.0.0.1:8770. It starts
only if every plug in the copy's config points at its fake, so nothing here can reach a
real plug. There is no API key, so no model is called. Sign in with the password "dev".
Ctrl+C stops it all.

The logger does not run here, so the copied readings age: 5 minutes after the copy, every
plug shows "no reading now". Copy again to see fresh power.

--write Plug-9 sets a device to write in the copy, to try the Devices page switch.
--plan saves a plan in the copy from code's own choice, in code's words (no model), to see
the Plan tab. Each fake plug starts with the timers the copy has for its plug, so Do it works.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # for tests.fake_tasmota

from agentc import config  # noqa: E402
from tests.fake_tasmota import FakePlug  # noqa: E402

DEV = ROOT / "dev"
PORT = 8770
SNAP = (
    "import sqlite3, sys; s = sqlite3.connect(sys.argv[1]);"
    " d = sqlite3.connect(sys.argv[2]); s.backup(d)"
)


GOAL_ONCE = """
import os
from datetime import UTC, datetime
from agentc import config, jobs, readings, store
cfg = config.load(os.environ["AGENTC_CONFIG"])
app = store.connect(cfg.app_db)
site = store.add_site(app, name=cfg.site, tz=cfg.tz)
jobs.goal_check(app, readings.connect(cfg.energy_db), cfg, site_id=site, now=datetime.now(UTC))
"""


PLAN_ONCE = """
import os
from datetime import UTC, datetime
from agentc import agent_tools, config, planner_agent, readings, store
cfg = config.load(os.environ["AGENTC_CONFIG"])
app = store.connect(cfg.app_db)
site = store.add_site(app, name=cfg.site, tz=cfg.tz)
now = datetime.now(UTC)
tools = agent_tools.Tools(app, readings.connect(cfg.energy_db), cfg, site, now)
t = planner_agent.PlanTools(tools, planner_agent.week_start(now, tz=cfg.tz))
choice = planner_agent.decide(t)
plan, _ = planner_agent._check({}, t, choice)  # code's words: no model here
saved = planner_agent.save(app, plan, site_id=site, cfg=cfg, now=now)
print(f"Plan: {len(plan.items)} items" if saved else "Plan: this week has one already")
"""


def fake_timers(fake: FakePlug, plug: str, data: Path) -> None:
    """Give a fake plug the timers the copy has stored for its plug, as Tasmota shows them."""
    from agentc import store  # noqa: PLC0415 (only here)

    app = store.connect(data / "app.db")
    if (mac := store.plug_mac(app, name=plug)) is None:
        return
    for t in store.load_timers(app, mac=mac):
        fake.timers[t.slot] = {"Enable": int(t.enable), "Mode": t.mode, "Time": t.time,
                               "Window": t.window, "Days": t.days, "Repeat": int(t.repeat),
                               "Output": t.output, "Action": t.action}  # fmt: skip
    fake.timers_on = store.timers_on(app, mac=mac)


def copy_from_hub(host: str, data: str) -> None:
    """A consistent copy of the hub's databases, made with SQLite's backup while it runs.
    The copy on the hub is removed even if fetching it fails."""
    (DEV / "data").mkdir(parents=True, exist_ok=True)
    for name in ("app.db", "energy.db"):
        snap = f"{data}/dev-copy-{name}"
        inside = f"python -c {shlex.quote(SNAP)} /data/{name} /data/dev-copy-{name}"
        try:
            run(["ssh", host, f"cd ~/agentc-edge && docker compose exec -T jobs {inside}"])
            run(["scp", "-q", f"{host}:{snap}", str(DEV / "data" / name)])
        finally:
            run(["ssh", host, f"rm -f {shlex.quote(snap)}"])
        print(f"Copied {name} from {host}.")


def plug_names(source: Path) -> list[str]:
    """Every plug in the config's [plugs] table, however its key is written."""
    return list(tomllib.loads(source.read_text()).get("plugs", {}))


def dev_config(source: Path, fakes: dict[str, str], write: list[str], out: Path,
               goal: float | None = None) -> Path:  # fmt: skip
    """The config with every plug pointed at its fake and --write devices set to write.
    Exits unless the written file loads and every plug in it is a fake."""
    text = source.read_text().rstrip("\n") + "\n"
    for plug, address in fakes.items():
        key = rf'(?:"{re.escape(plug)}"|{re.escape(plug)})'
        text = re.sub(rf"^(\s*{key}\s*=\s*)\"[^\"]*\"", rf'\1"{address}"', text, flags=re.M)
    for plug in write:
        section = f'[devices."{plug}"]'
        if section not in text:
            text += f'\n{section}\nname = "{plug}"\n'
        head, _, rest = text.partition(section + "\n")
        body, nxt, tail = rest.partition("\n[")
        body = "\n".join(x for x in body.split("\n") if not x.strip().startswith("access"))
        text = f'{head}{section}\naccess = "write"\n{body}{nxt}{tail}'
    if goal is not None:
        text = re.sub(r"(?m)^\[goal\]\n(?:(?!\[)[^\n]*\n)*", "", text)  # the copy's own goal
        text += f"\n[goal]\nmonth_kgco2e = {goal}\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    real = sorted(p for p, ip in config.load(out).plugs.items() if ip not in fakes.values())
    if real:
        out.unlink()
        sys.exit(f"Not started: {', '.join(real)} would point at a real plug. Check [plugs].")
    return out


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> None:
    ask = argparse.ArgumentParser(description="See the web app on your own computer.")
    ask.add_argument("--from-hub", metavar="HOST", help="copy the data from this SSH host first")
    ask.add_argument("--hub-data", default="/data", help="the data folder on the hub")
    ask.add_argument("--write", action="append", default=[], metavar="PLUG",
                     help="set this plug's device to write in the copy")  # fmt: skip
    ask.add_argument("--goal", type=float, metavar="KG", help="a monthly kgCO2e goal in the copy")
    ask.add_argument("--plan", action="store_true", help="save a plan in the copy (no model)")
    args = ask.parse_args()
    if args.from_hub:
        copy_from_hub(args.from_hub, args.hub_data)
    if not (DEV / "data" / "app.db").exists():
        sys.exit("No data yet. Run with --from-hub edge-hub first.")
    fakes = {}
    for n, plug in enumerate(plug_names(ROOT / "config.toml")):
        fake = FakePlug(power_w=5.0, mac=f"DE:VE:00:00:00:{n:02X}")
        fake.relay_on = True
        fake_timers(fake, plug, DEV / "data")
        fake.start()
        fakes[plug] = fake.ip
    cfg = dev_config(ROOT / "config.toml", fakes, args.write, DEV / "config.toml", args.goal)
    if args.goal is not None:  # the inbox shows what the daily goal job keeps: work it out once now
        once = {**os.environ, "AGENTC_CONFIG": str(cfg), "AGENTC_DATA": str(DEV / "data")}
        subprocess.run([sys.executable, "-c", GOAL_ONCE], check=True, env=once)
    if args.plan:
        once = {**os.environ, "AGENTC_CONFIG": str(cfg), "AGENTC_DATA": str(DEV / "data")}
        once.pop("ANTHROPIC_API_KEY", None)
        subprocess.run([sys.executable, "-c", PLAN_ONCE], check=True, env=once)
    env = {**os.environ, "AGENTC_CONFIG": str(cfg), "AGENTC_DATA": str(DEV / "data"),
           "AGENTC_SECRET": "dev-only", "AGENTC_PASSWORD": "dev", "AGENTC_HUB": ""}  # fmt: skip
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
