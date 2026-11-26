"""Agent evals (docs/evaluation.md): real nights in, code scorers on what comes out.

A live run asks the real model and records every step's reply in evals/recorded.json.
Every commit replays those replies for free. If the prompt, the tools or the brief have
changed since, the replies are stale and the replay stops: run the live evals again.
"""

import csv
import gzip
import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agentc import (
    agent,
    assistant,
    calc,
    config,
    house,
    planner,
    planner_agent,
    review,
    setup,
    store,
    watcher,
    words,
)
from agentc.agent_tools import Tools, card
from agentc.config import Config, Device, Goal, Tier
from agentc.devices.base import Timer
from agentc.house import Card
from agentc.llm import Block, Llm, Message, OnText, Schema, StepSend
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
    """Asks, and offers to switch it off: it is still drawing power."""
    return d is not None and d.action == "ask" and any(
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


def run(send: StepSend | None, *, samples: int = 5, path: Path | None = None,
        cases: Sequence[str] = ()) -> list[Score]:  # fmt: skip
    """Live with `send`, saving the replies to `path`; with None, replay what `path` holds.
    cases: with `send`, ask the model only for these and replay the rest (the Planner's
    cases cost more, so they are recorded on their own)."""
    path = path or RECORDED
    cfg = settings()
    saved = json.loads(path.read_text()) if path.exists() else {}

    def live(eid: str) -> StepSend | None:
        return send if send and (not cases or eid in cases) else None

    scores = []
    try:
        for ev in LIVE:
            signal, tools = case(ev, cfg)
            look = lambda s, ev=ev, signal=signal: _sample(ev, signal, s, cfg)  # noqa: E731
            scores.append(_score(ev.id, key(signal, ev, tools), saved, live(ev.id), samples, cfg,
                                 look))  # fmt: skip
        scores.append(_score(SETUP_ID, setup_key(setup_case(cfg), cfg), saved, live(SETUP_ID),
                             samples, cfg, lambda s: _setup_sample(s, cfg)))  # fmt: skip
        for pe in PLANS:
            pcfg, tools = plan_case(pe, cfg)
            plan = lambda s, pe=pe: _plan_sample(pe, s, cfg)  # noqa: E731
            scores.append(_score(pe.id, plan_key(tools), saved, live(pe.id), samples, pcfg, plan,
                                 tier="mid"))  # fmt: skip
        for ae in ASKS:
            _, t = review_case(cfg)
            ask = lambda s, ae=ae: _ask_sample(ae, s, cfg)  # noqa: E731
            scores.append(_score(ae.id, ask_key(t, ae), saved, live(ae.id), samples, cfg, ask))
        for re_ in REVIEWS:
            _, t = review_case(cfg)
            talk = lambda s, re_=re_: _review_sample(re_, s, cfg)  # noqa: E731
            scores.append(_score(re_.id, review_key(t, re_), saved, live(re_.id), samples, cfg,
                                 talk, tier=re_.scope.tier))  # fmt: skip
    finally:  # what was asked live is kept, even if a later case cannot be replayed
        if send:
            path.write_text(json.dumps(saved, indent=1) + "\n")
    return scores


def _score(
    eid: str, k: str, saved: dict[str, Any], send: StepSend | None, samples: int, cfg: Config,
    sample: Callable[[StepSend], Sample], tier: str = "small",
) -> Score:  # fmt: skip
    """One case: live (recording into `saved`) or replayed from it."""
    if send:
        got = [sample(send) for _ in range(samples)]
        saved[eid] = {"key": k, "model": cfg.tiers[tier].model,
                      "samples": [x.steps for x in got]}  # fmt: skip
    elif eid not in saved:
        raise Stale(f"{eid}: not recorded yet. Run `agentc eval --live --case {eid}`.")
    elif saved[eid]["key"] != k:
        raise Stale(f"{eid}: the prompt changed. Run `agentc eval --live` again.")
    else:
        got = [sample(_replay(steps)) for steps in saved[eid]["samples"]]
    return Score(eid, got)


# EV-13: the setup chat, on the LG TV's real readings, with an owner who says the same things


SETUP_ID = "EV-13"
OWNER = ("The LG TV, in the living room.",
         "Nothing else is on that plug, and it does not need to stay on.",
         "That is all I know.")  # fmt: skip


def _setup_right(d: setup.Draft | None, used: Sequence[str]) -> bool:
    """Media, in the living room, not must-run: what the owner said. And it read the usual
    week, to suggest when the TV is on (FM-08)."""
    return (d is not None and d.card.kind == "media" and "living" in d.card.room.lower()
            and d.card.must_run is None and "usual" in used)  # fmt: skip


def setup_case(cfg: Config) -> Tools:
    """The house of EV-22 (12 Nov at 12:30), with the TV's card empty: a new plug."""
    _, tools = case(LIVE[2], cfg)
    store.save_card(tools.app, site_id=1, mac=TV[1], card=house.Card(), now=tools.now)
    return tools


def setup_key(tools: Tools, cfg: Config) -> str:
    """Changes when the setup agent's prompt, tools, brief or the owner's words change."""
    text = setup.VERSION + setup.SYSTEM + json.dumps(setup.TOOLS, sort_keys=True)
    text += card(tools.app, cfg, TV[0]) + json.dumps(OWNER)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _setup_sample(send: StepSend, cfg: Config) -> Sample:
    tools = setup_case(cfg)
    steps: list[list[Block]] = []

    def listen(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema],
               on_text: OnText | None = None) -> tuple[list[Block], str, int, int]:  # fmt: skip
        blocks, stop, tokens_in, tokens_out = send(tier=tier, system=system, messages=messages,
                                                   tools=tools)  # fmt: skip
        steps.append(blocks)
        return blocks, stop, tokens_in, tokens_out

    brain = Llm(tools.app, 1, cfg.tz, cfg.tiers, cfg.budget, listen, clock=lambda: tools.now)
    talk = [("agent", setup.greeting("LG TV", house.Card()))]
    draft, used, earlier = None, [], []
    for said in OWNER:  # up to 3 owner messages
        talk.append(("owner", said))
        r = setup.reply(talk, TV[0], tools=tools, llm=brain, run_id=SETUP_ID, earlier=earlier)
        used += r.tools
        earlier += r.reads
        talk.append(("agent", r.text))
        if (draft := r.draft) is not None:
            break
    return Sample(steps, "card" if draft else "none", _setup_right(draft, used), used,
                  store.llm_usd(tools.app, site_id=1))  # fmt: skip


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
    """The house as it was when the agent looks, and the signal it gets."""
    tools, rows = house_at(ev.at, cfg)
    seen = [(at, w) for at, w in rows[ev.plug[0]] if at < ev.at]
    peak = max(w for at, w in seen if at >= ev.start)
    minutes = round((ev.at - ev.start) / timedelta(minutes=1))
    e = watcher.Event(ev.kind, ev.plug[1], ev.start, {"peak_w": peak, "minutes": minutes})
    name = config.device(cfg, ev.plug[0]).name
    signal = words.event_words(e, {ev.plug[1]: name}, tz=cfg.tz)
    return signal, tools


def house_at(at: datetime, cfg: Config) -> tuple[Tools, dict[str, list[tuple[datetime, float]]]]:
    """The house of the real readings, as of `at`, with each plug's readings.

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
                        now=at)  # fmt: skip
        store.save_card(app, site_id=1, mac=mac, card=CARDS[plug], now=at)
        profile = calc.build_profile(rows[plug], tz=cfg.tz) or []
        store.save_profile(app, site_id=1, mac=mac, profile=profile)
        rs = [(at.astimezone(UTC).isoformat(), plug, w) for at, w in rows[plug]]
        energy.executemany("INSERT INTO readings VALUES (?, ?, ?, NULL)", rs)
    return Tools(app, energy, cfg, site_id=1, now=at), rows


def key(signal: str, ev: Eval, tools: Tools) -> str:
    """Changes when anything the model reads first changes: the prompt, tools or brief."""
    text = agent.VERSION + agent.SYSTEM + json.dumps(agent.TOOLS, sort_keys=True)
    text += agent.brief(signal, ev.plug[0], tools)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _sample(ev: Eval, signal: str, send: StepSend, cfg: Config) -> Sample:
    _, tools = case(ev, cfg)  # a fresh house each time: nothing carries over
    steps: list[list[Block]] = []

    def listen(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema],
               on_text: OnText | None = None) -> tuple[list[Block], str, int, int]:  # fmt: skip
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

    def send(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema],
             on_text: OnText | None = None) -> tuple[list[Block], str, int, int]:  # fmt: skip
        return left.pop(0), "tool_use", 0, 0

    return send


# EV-30 and EV-31: the weekly Planner, on the TV and the PeoTV box's real readings


@dataclass(frozen=True)
class PlanEval:
    id: str
    access: str  # the TV's and the PeoTV box's access
    goal_kg: float  # the month's goal: the eval house heads to about 4.4 kgCO2e
    right: Callable[[planner_agent.Plan], bool]


SUNDAY = datetime(2026, 11, 15, 2, 30, tzinfo=ZoneInfo(TZ))  # the readings end here
ON6 = Timer(1, True, 0, "06:00", 0, "1111111", True, 1, 1)
OFF1 = Timer(2, True, 0, "01:00", 0, "1111111", True, 1, 0)


def _kept_all(p: planner_agent.Plan) -> bool:
    """It explained every change code chose, in its own checked words: no item fell back to
    code's words and none was left out."""
    return (p.done and bool(p.chosen) and len(p.items) == len(p.chosen) and not p.blocked
            and not p.plain)  # fmt: skip


def _nothing(p: planner_agent.Plan) -> bool:
    """On track and nothing set to write: code chose nothing, and the plan is empty."""
    return p.done and not p.chosen and not p.items and not p.blocked


def _says_shortfall(p: planner_agent.Plan) -> bool:
    """The goal can't be reached: the summary says by how much, with code's number."""
    if not p.shortfall:
        return False
    kg = p.shortfall.split(", ")[-1].split(" kgCO2e")[0]
    return _kept_all(p) and re.search(rf"(?<![\d.]){re.escape(kg)}(?!\d)", p.summary) is not None


PLANS = (
    PlanEval("EV-30", "write", 3.7, _kept_all),  # reachable: the printer action closes it
    PlanEval("EV-31", "advice", 50.0, _nothing),  # on track; nothing set to write
    PlanEval("EV-32", "write", 0.5, _says_shortfall),  # can't be reached: say so
)


def plan_case(pe: PlanEval, cfg: Config) -> tuple[Config, Tools]:
    """The house on Sunday 15 Nov: the TV and the PeoTV box with their real timers (on at
    06:00, off at 01:00), both at the access the case says, and the case's goal."""
    pcfg = replace(cfg, goal=Goal(pe.goal_kg),
                   devices={**cfg.devices, TV[0]: Device("LG TV", access=pe.access),
                            BOX[0]: Device("PeoTV box", access=pe.access)})  # fmt: skip
    tools, _ = house_at(SUNDAY, pcfg)
    for _, mac in (TV, BOX):
        store.save_timers(tools.app, site_id=1, mac=mac, timers=[ON6, OFF1], now=SUNDAY)
    return pcfg, tools


def plan_key(tools: Tools) -> str:
    """Changes when what the Planner reads changes: its prompt, tools, the brief (the goal,
    the week and code's choice) and the notes."""
    t = planner_agent.PlanTools(tools, planner_agent.week_start(tools.now, tz=TZ))
    text = planner_agent.VERSION + planner_agent.SYSTEM + json.dumps(planner_agent.TOOLS,
                                                                     sort_keys=True)  # fmt: skip
    text += planner_agent.decide(t).brief + t.week_notes()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _plan_sample(pe: PlanEval, send: StepSend, cfg: Config) -> Sample:
    pcfg, tools = plan_case(pe, cfg)
    steps: list[list[Block]] = []

    def listen(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema],
               on_text: OnText | None = None) -> tuple[list[Block], str, int, int]:  # fmt: skip
        blocks, stop, tokens_in, tokens_out = send(tier=tier, system=system, messages=messages,
                                                   tools=tools)  # fmt: skip
        steps.append(blocks)
        return blocks, stop, tokens_in, tokens_out

    brain = Llm(tools.app, 1, pcfg.tz, pcfg.tiers, pcfg.budget, listen, clock=lambda: SUNDAY)
    p = planner_agent.plan(tools, llm=brain, run_id=pe.id)
    action = f"items {len(p.items)}" if p.done else "none"
    return Sample(steps, action, pe.right(p), list(p.tools), store.llm_usd(tools.app, site_id=1))


# EV-33 to EV-35: the weekly review chat (5c), on the EV-30 house with a plan of three items:
# the box's start moved later (ready), the TV's off moved to 23:30 (it cut real use on 3 of
# 14 days, so only to talk over), and the printer's wall switch.

BOX_MOVE = planner.Change("move_timer", slot=1, time="09:30", from_time="06:00")
TV_MOVE = planner.Change("move_timer", slot=2, time="23:30", from_time="01:00")


@dataclass(frozen=True)
class ReviewEval:
    id: str
    scope: review.Scope
    owner: tuple[str, ...]  # what the owner says, in turn, until a draft
    right: Callable[[review.Draft | None, list[str]], bool]  # (the draft, its words)


def _keeps_for_weekends(d: review.Draft | None, said: list[str]) -> bool:
    """It waits for the owner's decision (the second message), then keeps the TV's off time
    with the owner's reason about weekends."""
    return (len(said) == 2 and d is not None and d.action == "keep" and d.item == 2
            and "weekend" in d.reason.lower())  # fmt: skip


def _does_it_anyway(d: review.Draft | None, said: list[str]) -> bool:
    """It first tells the owner the days the change would have got in the way (3 of 14),
    and only after the owner says go ahead drafts the TV's own change, anyway."""
    return (len(said) == 2 and "3" in said[0] and d is not None and d.action == "anyway"
            and d.item == 2)  # fmt: skip


def _answers_about_the_printer(d: review.Draft | None, said: list[str]) -> bool:
    """A question, not a decision: no draft, and the right answer, with code's number: the
    printer's wall switch, about 3.55 kWh a month."""
    return d is None and len(said) == 1 and "printer" in said[0].lower() and "3.55" in said[0]


REVIEWS = (
    ReviewEval("EV-33", review.Scope(1, 2), (
        "We watch TV late at weekends.", "Leave it as it is then."), _keeps_for_weekends),
    ReviewEval("EV-34", review.Scope(1, 2), (
        "What would it cut?", "That is fine, the late nights do not matter. Do it anyway."),
        _does_it_anyway),
    ReviewEval("EV-35", review.Scope(1, None), (
        "Which change saves the most, and why?",), _answers_about_the_printer),
)  # fmt: skip


def review_case(cfg: Config) -> tuple[Config, planner_agent.PlanTools]:
    """The EV-30 house on Sunday 15 Nov, with this week's plan of three items saved."""
    pcfg, tools = plan_case(PLANS[0], cfg)
    store.add_plan(tools.app, site_id=1, week="2026-11-09", summary="Three changes.",
                   now=SUNDAY, items=[
        (BOX[1], BOX_MOVE.to_json(), "Nobody uses it before 09:30.", "Saves about 0.08 kWh.",
         True, "On at 06:00, every day", "On at 09:30, every day"),
        (TV[1], TV_MOVE.to_json(), "It idles late.", "Saves about 0.06 kWh. In use in the new"
         " off time on 3 of 14 days.", False, "Off at 01:00, every day",
         "Off at 23:30, every day"),
        (PRINTER[1], planner_agent.action_json(planner_agent.ACTION), "It idles.",
         "Saves about 3.55 kWh.", True, "", planner_agent.ACTION)])  # fmt: skip
    return pcfg, review.tools_for(tools)


def review_key(t: planner_agent.PlanTools, re_: ReviewEval) -> str:
    """Changes when the review agent's prompt, tools, brief or the owner's words change."""
    text = review.VERSION + review.SYSTEM + json.dumps(review.TOOLS, sort_keys=True)
    text += review.brief(t, re_.scope) + review.greeting(t, re_.scope) + json.dumps(re_.owner)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _review_sample(re_: ReviewEval, send: StepSend, cfg: Config) -> Sample:
    pcfg, t = review_case(cfg)
    steps: list[list[Block]] = []

    def listen(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema],
               on_text: OnText | None = None) -> tuple[list[Block], str, int, int]:  # fmt: skip
        blocks, stop, tokens_in, tokens_out = send(tier=tier, system=system, messages=messages,
                                                   tools=tools)  # fmt: skip
        steps.append(blocks)
        return blocks, stop, tokens_in, tokens_out

    brain = Llm(t.base.app, 1, pcfg.tz, pcfg.tiers, pcfg.budget, listen, clock=lambda: SUNDAY)
    talk = [("agent", review.greeting(t, re_.scope))]
    draft, used, earlier, said = None, [], [], []
    for words_ in re_.owner:
        talk.append(("owner", words_))
        r = review.reply(talk, re_.scope, tools=t, llm=brain, run_id=re_.id, earlier=earlier)
        used += r.tools
        earlier += r.reads
        said.append(r.text)
        talk.append(("agent", r.text))
        if (draft := r.draft) is not None:
            break
    lost = any(x == review.LOST or x.startswith("Sorry") for x in said)
    action = "none" if lost else draft.action if draft else "answer"
    return Sample(steps, action, re_.right(draft, said), used,
                  store.llm_usd(t.base.app, site_id=1))  # fmt: skip


# EV-36 to EV-38: the Assistant (5d), on the same house, with the owner asking once.


@dataclass(frozen=True)
class AskEval:
    id: str
    said: str
    right: Callable[[assistant.Draft | None, str, list[str]], bool]  # (draft, words, tools)


def _friday_habit(d: assistant.Draft | None, said: str, tools: list[str]) -> bool:
    """A habit for the TV on Friday nights only, from the evening to 01:00: no rule, no
    timer. A window that ends next day starts later than it ends."""
    h = d.habit if d else None
    return (d is not None and d.action == "habit" and d.device == "LG TV" and h is not None
            and h.days == "0000010" and h.end == "01:00" and h.start > h.end)  # fmt: skip


DEVICES = ("Desk 2", "3D printer", "LG TV", "PeoTV box")


def _desk_2_most(d: assistant.Draft | None, said: str, tools: list[str]) -> bool:
    """A question: no draft; it worked out what each used (cost), and named Desk 2 first."""
    named = sorted((said.find(x), x) for x in DEVICES if x in said)
    return d is None and "cost" in tools and bool(named) and named[0][1] == "Desk 2"


def _box_off(d: assistant.Draft | None, said: str, tools: list[str]) -> bool:
    return d is not None and d.action == "switch_off" and d.device == "PeoTV box"


ASKS = (
    AskEval("EV-36", "Keep the LG TV on until 1 on Fridays, we watch late.", _friday_habit),
    AskEval("EV-37", "What used the most this week?", _desk_2_most),
    AskEval("EV-38", "Switch off the PeoTV box now.", _box_off),
)


def ask_key(t: planner_agent.PlanTools, ae: AskEval) -> str:
    """Changes when the Assistant's prompt, tools, brief or the owner's words change."""
    text = assistant.VERSION + assistant.SYSTEM + json.dumps(assistant.TOOLS, sort_keys=True)
    text += assistant.brief(t) + assistant.GREETING + ae.said
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _ask_sample(ae: AskEval, send: StepSend, cfg: Config) -> Sample:
    pcfg, t = review_case(cfg)
    steps: list[list[Block]] = []

    def listen(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema],
               on_text: OnText | None = None) -> tuple[list[Block], str, int, int]:  # fmt: skip
        blocks, stop, tokens_in, tokens_out = send(tier=tier, system=system, messages=messages,
                                                   tools=tools)  # fmt: skip
        steps.append(blocks)
        return blocks, stop, tokens_in, tokens_out

    brain = Llm(t.base.app, 1, pcfg.tz, pcfg.tiers, pcfg.budget, listen, clock=lambda: SUNDAY)
    talk = [("agent", assistant.GREETING), ("owner", ae.said)]
    r = assistant.reply(talk, tools=t, llm=brain, run_id=ae.id)
    lost = r.text in (review.LOST, review.loop.STUCK)
    action = "none" if lost else r.draft.action if r.draft else "answer"
    return Sample(steps, action, ae.right(r.draft, r.text, list(r.tools)), list(r.tools),
                  store.llm_usd(t.base.app, site_id=1))  # fmt: skip
