"""Agent evals (docs/evaluation.md): fixed cases in, code scorers on what comes out.

A live run asks the real model and records its answers in evals/recorded.json. Every
commit replays those answers for free. If anything the model reads has changed since,
the answers are stale and the replay stops: run the live evals again.
"""

import csv
import gzip
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from agentc import calc, config, responder, store, watcher
from agentc.config import Config, Device, Rule, Tier
from agentc.llm import Llm, Schema, Send
from agentc.options import Money, Proposal

ROOT = Path(__file__).resolve().parents[2]
RECORDED = ROOT / "evals" / "recorded.json"
TZ = "Asia/Colombo"
NOW = datetime(2026, 11, 15, 6, 0, tzinfo=ZoneInfo(TZ))  # desk2.csv.gz ends here
LATE = Rule("latest_off", "02:00", hard=False, text="Desk 2 may run until 02:00 on work nights")


class Stale(Exception):
    """What the model reads changed after its answers were recorded."""


@dataclass(frozen=True)
class Eval:
    id: str
    device: Device
    right: Callable[[Proposal | None, str], bool]  # given the proposal (or None) and the reason


LIVE = (
    Eval("EV-01", Device("Desk 2"), lambda p, _: p is not None and [
        (c.action, c.time) for c in p.changes] == [("off", "01:30"), ("on", "07:00")]),
    Eval("EV-03", Device("Desk 2", rules=(LATE,)), lambda p, why: p is None and "02:00" in why),
)  # fmt: skip


@dataclass(frozen=True)
class Sample:
    """One go at a case: what the model said, and how the code scored it."""

    answers: list[str]  # the first answer, and the retry's if there was one
    right: bool  # the expected choice
    checked: bool  # the final answer passed every check, every number grounded among them
    parsed: bool  # every answer was the JSON asked for
    option: str  # the option picked in the end; "?" if it was not JSON
    usd: float


@dataclass(frozen=True)
class Score:
    id: str
    samples: list[Sample]

    @property
    def passed(self) -> bool:
        """At least 90% right, and 100% checked and parsed (thresholds in testing.md)."""
        s = self.samples
        return sum(x.right for x in s) >= 0.9 * len(s) and all(x.checked and x.parsed for x in s)

    def line(self) -> str:
        """One line a case, e.g. "EV-01: right 9/10, checks 10/10, ..., USD 0.0013: PASS"."""
        s, n = self.samples, len(self.samples)
        right, checked, parsed = (sum(x.right for x in s), sum(x.checked for x in s),
                                  sum(x.parsed for x in s))  # fmt: skip
        picked = ", ".join(f"{k} {v}" for k, v in sorted(Counter(x.option for x in s).items()))
        return (f"{self.id}: right {right}/{n}, checks {checked}/{n}, format {parsed}/{n},"
                f" picked {picked}, USD {sum(x.usd for x in s):.4f}:"
                f" {'PASS' if self.passed else 'FAIL'}")  # fmt: skip


def run(send: Send | None, *, samples: int = 10, path: Path | None = None) -> list[Score]:
    """Live with `send`, saving the answers to `path`; with None, replay what `path` holds."""
    path = path or RECORDED
    cfg = settings()
    saved = {} if send else json.loads(path.read_text())
    scores = []
    for ev in LIVE:
        c = case(ev.device)
        if send:
            got = [_sample(c, ev, send, cfg) for _ in range(samples)]
            saved[ev.id] = {"key": key(c), "model": cfg.tiers["small"].model,
                            "answers": [x.answers for x in got]}  # fmt: skip
        elif saved[ev.id]["key"] != key(c):
            raise Stale(f"{ev.id}: the prompt changed. Run `agentc eval --live` again.")
        else:
            got = [_sample(c, ev, _replay(a), cfg) for a in saved[ev.id]["answers"]]
        scores.append(Score(ev.id, got))
    if send:
        path.write_text(json.dumps(saved, indent=1) + "\n")
    return scores


def settings() -> Config:
    """The example config: the evals must give the same facts on every machine."""
    return config.load(ROOT / "config.example.toml")


def readings() -> list[tuple[datetime, float]]:
    """Desk 2's real readings, 1 to 15 Nov 2026: times and watts only (NFR-14 privacy)."""
    with gzip.open(ROOT / "evals" / "desk2.csv.gz", "rt") as f:
        return [(datetime.fromisoformat(r["ts_utc"]), float(r["watts"])) for r in csv.DictReader(f)]


def case(device: Device) -> responder.Case:
    """The first night in the readings that the Responder can offer a fix for (2 Nov)."""
    cfg, rs = settings(), readings()
    usual = watcher.usual_week(calc.build_profile(rs, tz=cfg.tz) or [])
    money = Money(cfg.tariff, cfg.month_kwh, cfg.levy, cfg.factor)
    cases = (
        responder.gather(run, mac="desk2", device=device, readings=rs, usual=usual, timers=[],
                         money=money, rejected=[], tz=cfg.tz, days=(NOW - rs[0][0]).days)
        for run in watcher.unusual_runs(rs, usual, tz=cfg.tz)
    )  # fmt: skip
    return next(c for c in cases if c)


def key(c: responder.Case) -> str:
    """Changes when anything the model reads changes: the system prompt, facts or schema."""
    text = responder.SYSTEM + responder.describe(c, tz=TZ)
    text += json.dumps(responder.schema(c), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _sample(c: responder.Case, ev: Eval, send: Send, cfg: Config) -> Sample:
    db = store.connect(":memory:")
    store.add_site(db, name="eval", tz=cfg.tz)
    said: list[str] = []

    def listen(*, tier: Tier, system: str, prompt: str, schema: Schema) -> tuple[str, int, int]:
        text, tokens_in, tokens_out = send(tier=tier, system=system, prompt=prompt, schema=schema)
        said.append(text)
        return text, tokens_in, tokens_out

    llm = Llm(db, 1, cfg.tz, cfg.tiers, cfg.budget, listen, clock=lambda: NOW)
    p = responder.respond(c, llm=llm, db=db, site_id=1, event_id=None, run_id=ev.id, tz=cfg.tz,
                          now=NOW)  # fmt: skip
    ((action, detail),) = db.execute("SELECT action, detail FROM audit").fetchall()
    why = p.reason if p else json.loads(detail).get("reason", "")
    options = [_option(t) for t in said]
    return Sample(said, ev.right(p, why), action != "draft_dropped", "?" not in options,
                  options[-1], store.llm_usd(db, site_id=1))  # fmt: skip


def _option(text: str) -> str:
    try:
        return responder.Answer.model_validate_json(text).option
    except ValidationError:
        return "?"


def _replay(answers: list[str]) -> Send:
    """A stand-in model that gives the recorded answers in turn, at no cost."""
    left = list(answers)

    def send(*, tier: Tier, system: str, prompt: str, schema: Schema) -> tuple[str, int, int]:
        return left.pop(0), 0, 0

    return send
