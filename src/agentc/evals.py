"""Agent evals (docs/evaluation.md): real nights in, code scorers on what comes out.

A live run asks the real model and records every step's reply in evals/recorded.json.
Every commit replays those replies for free. If the prompt, the tools or the brief have
changed since, the replies are stale and the replay stops: run the live evals again.
"""

import csv
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agentc import agent, calc, config, store, watcher, words
from agentc.agent_tools import Tools
from agentc.config import Config, Device, Tier
from agentc.house import Card
from agentc.llm import Block, Llm, Message, Schema, StepSend
from agentc.owner import Decision

ROOT = Path(__file__).resolve().parents[2]
RECORDED = ROOT / "evals" / "recorded.json"
TZ = "Asia/Colombo"
DESK, PRINTER = ("Plug-1", "AA:00:00:00:00:01"), ("Plug-2", "AA:00:00:00:00:02")
TV, BOX = ("Plug-3", "AA:00:00:00:00:03"), ("Plug-4", "AA:00:00:00:00:04")
CARDS = {
    DESK[0]: Card("computer", "desk 2", "monitor and speakers",
                  "An iPhone sleep shortcut switches it off at night"),
    PRINTER[0]: Card("printer", "workshop", "3D printer and its Raspberry Pi"),
    TV[0]: Card("media", "living room", "LG TV"),
    BOX[0]: Card("media", "living room", "PeoTV box (IPTV)"),
}  # fmt: skip


class Stale(Exception):
    """What the model reads changed after its replies were recorded."""


@dataclass(frozen=True)
class Eval:
    id: str
    plug: tuple[str, str]  # (plug name, MAC)
    kind: str  # the signal: left_on or unusual_use
    start: datetime  # when the signal's run started (local)
    at: datetime  # when the agent looks (local)
    right: Callable[[Decision | None], bool]


def _stops(d: Decision | None) -> bool:
    """Asks or proposes, and offers to switch it off: it is still drawing power."""
    return d is not None and d.action in ("ask", "propose") and any(
        c.does == "switch_off_now" for c in d.choices)  # fmt: skip


def _quiet(d: Decision | None) -> bool:
    """Ignore or note: no question."""
    return d is not None and d.action in ("ignore", "note")


MONDAY = datetime(2026, 11, 2, tzinfo=ZoneInfo(TZ))
THURSDAY = MONDAY + timedelta(days=10)  # 12 Nov
LIVE = (
    # Desk 2 left on at about 5 W after a late session; the real 1 to 2 Nov night
    Eval("EV-01", DESK, "left_on", MONDAY.replace(hour=1, minute=37),
         MONDAY.replace(hour=2, minute=10), _stops),
    # the 3D printer's Pi on at night: access none, so only ignore or note
    Eval("EV-05", PRINTER, "left_on", MONDAY.replace(hour=1), MONDAY.replace(hour=2, minute=10),
         _quiet),
    # the LG TV on at midday with the PeoTV box: someone is watching (FM-05)
    Eval("EV-22", TV, "unusual_use", THURSDAY.replace(hour=11, minute=46),
         THURSDAY.replace(hour=12, minute=30), _quiet),
)  # fmt: skip


@dataclass(frozen=True)
class Sample:
    """One go at a case: every step the model sent, and how the code scored it."""

    steps: list[list[Block]]
    action: str  # the final decision's action; "none" if no decision passed the checks
    right: bool
    tools: list[str]
    usd: float


@dataclass(frozen=True)
class Score:
    id: str
    samples: list[Sample]

    @property
    def passed(self) -> bool:
        """At least 80% right, and every sample decided (so passed every check)."""
        s = self.samples
        return sum(x.right for x in s) >= 0.8 * len(s) and all(x.action != "none" for x in s)

    def line(self) -> str:
        """One line a case, e.g. "EV-01: right 5/5, actions ask 5, ..., USD 0.0050: PASS"."""
        s, n = self.samples, len(self.samples)
        actions = ", ".join(f"{k} {v}" for k, v in sorted(Counter(x.action for x in s).items()))
        steps = sum(len(x.steps) for x in s) / n
        return (f"{self.id}: right {sum(x.right for x in s)}/{n}, actions {actions},"
                f" {steps:.1f} steps each, USD {sum(x.usd for x in s):.4f}:"
                f" {'PASS' if self.passed else 'FAIL'}")  # fmt: skip


def run(send: StepSend | None, *, samples: int = 5, path: Path | None = None) -> list[Score]:
    """Live with `send`, saving the replies to `path`; with None, replay what `path` holds."""
    path = path or RECORDED
    cfg = settings()
    saved = {} if send else json.loads(path.read_text())
    scores = []
    for ev in LIVE:
        signal, tools = case(ev, cfg)
        k = key(signal, ev, tools)
        if send:
            got = [_sample(ev, signal, send, cfg) for _ in range(samples)]
            saved[ev.id] = {"key": k, "model": cfg.tiers["small"].model,
                            "samples": [x.steps for x in got]}  # fmt: skip
        elif saved[ev.id]["key"] != k:
            raise Stale(f"{ev.id}: the prompt changed. Run `agentc eval --live` again.")
        else:
            got = [_sample(ev, signal, _replay(steps), cfg) for steps in saved[ev.id]["samples"]]
        scores.append(Score(ev.id, got))
    if send:
        path.write_text(json.dumps(saved, indent=1) + "\n")
    return scores


def settings() -> Config:
    """The example config, plus the TV and the PeoTV box: the same facts on every machine."""
    cfg = config.load(ROOT / "config.example.toml")
    devices = {TV[0]: Device("LG TV"), BOX[0]: Device("PeoTV box")}
    return replace(cfg, plugs={**cfg.plugs, TV[0]: "192.0.2.13", BOX[0]: "192.0.2.14"},
                   devices={**cfg.devices, **devices})  # fmt: skip


def readings(name: str = "desk2") -> list[tuple[datetime, float]]:
    """Real readings of one plug, 1 to 15 Nov 2026: times and watts only (NFR-14 privacy).

    desk2: Desk 2. tv: the LG TV. box: the PeoTV box.
    """
    with gzip.open(ROOT / "evals" / f"{name}.csv.gz", "rt") as f:
        return [(datetime.fromisoformat(r["ts_utc"]), float(r["watts"])) for r in csv.DictReader(f)]


def printer(desk: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """The 3D printer plug: 0 W, but its Pi draws 5 W from 01:00 on 2 Nov (made up)."""
    on = MONDAY.replace(hour=1)
    return [(at, 5.0 if at >= on else 0.0) for at, _ in desk]


def case(ev: Eval, cfg: Config) -> tuple[str, Tools]:
    """The house as it was when the agent looks, and the signal it gets.

    Profiles come from all 14 days, so they know a little more than the house did then.
    """
    desk = readings()
    rows = {DESK[0]: desk, PRINTER[0]: printer(desk), TV[0]: readings("tv"),
            BOX[0]: readings("box")}  # fmt: skip
    app = store.connect(":memory:")
    store.add_site(app, name="eval", tz=cfg.tz)
    energy = sqlite3.connect(":memory:")
    energy.execute("CREATE TABLE readings (ts_utc TEXT, plug TEXT, power_w REAL, error TEXT)")
    for plug, mac in (DESK, PRINTER, TV, BOX):
        store.plug_seen(app, site_id=1, mac=mac, name=plug, ip=cfg.plugs[plug], timers_on=True,
                        now=ev.at)  # fmt: skip
        store.save_card(app, site_id=1, mac=mac, card=CARDS[plug], now=ev.at)
        profile = calc.build_profile(rows[plug], tz=cfg.tz) or []
        store.save_profile(app, site_id=1, mac=mac, profile=profile)
        rs = [(at.astimezone(UTC).isoformat(), plug, w) for at, w in rows[plug]]
        energy.executemany("INSERT INTO readings VALUES (?, ?, ?, NULL)", rs)
    seen = [(at, w) for at, w in rows[ev.plug[0]] if at < ev.at]
    peak = max(w for at, w in seen if at >= ev.start)
    minutes = round((ev.at - ev.start) / timedelta(minutes=1))
    e = watcher.Event(ev.kind, ev.plug[1], ev.start, {"peak_w": peak, "minutes": minutes})
    name = config.device(cfg, ev.plug[0]).name
    signal = words.event_words(e, {ev.plug[1]: name}, tz=cfg.tz)
    return signal, Tools(app, energy, cfg, site_id=1, now=ev.at)


def key(signal: str, ev: Eval, tools: Tools) -> str:
    """Changes when anything the model reads first changes: the prompt, tools or brief."""
    text = agent.VERSION + agent.SYSTEM + json.dumps(agent.TOOLS, sort_keys=True)
    text += agent.brief(signal, ev.plug[0], tools)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _sample(ev: Eval, signal: str, send: StepSend, cfg: Config) -> Sample:
    _, tools = case(ev, cfg)  # a fresh house each time: nothing carries over
    steps: list[list[Block]] = []

    def listen(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema]
               ) -> tuple[list[Block], str, int, int]:  # fmt: skip
        blocks, stop, tokens_in, tokens_out = send(tier=tier, system=system, messages=messages,
                                                   tools=tools)  # fmt: skip
        steps.append(blocks)
        return blocks, stop, tokens_in, tokens_out

    llm = Llm(tools.app, 1, cfg.tz, cfg.tiers, cfg.budget, listen, clock=lambda: ev.at)
    look = agent.look(signal, ev.plug[0], tools=tools, llm=llm, run_id=ev.id)
    d = look.decision
    return Sample(steps, d.action if d else "none", ev.right(d), look.tools,
                  store.llm_usd(tools.app, site_id=1))  # fmt: skip


def _replay(steps: list[list[Block]]) -> StepSend:
    """A stand-in model that sends the recorded steps in turn, at no cost."""
    left = list(steps)

    def send(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema]
             ) -> tuple[list[Block], str, int, int]:  # fmt: skip
        return left.pop(0), "tool_use", 0, 0

    return send
