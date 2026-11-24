import json
from datetime import UTC, datetime

import anthropic
import httpx2 as httpx  # the HTTP library the anthropic SDK uses
import pytest
from anthropic.types import Message

from agentc import llm, store
from agentc.config import Budget, Tier

NOW = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
SMALL = Tier("small-model", usd_in=1.0, usd_out=10.0, effort="low")
CAPS = Budget(month_usd=1.0, run_usd=0.05)
OK = [{"type": "text", "text": "ok"}]


class FakeModel:
    """Stands in for the provider: notes each step, answers with fixed blocks and tokens."""

    def __init__(self, tokens=(1000, 100)):
        self.tokens, self.calls = tokens, []

    def __call__(self, *, tier, system, messages, tools):
        self.calls.append({"model": tier.model, "system": system, "messages": messages})
        return OK, "end_turn", *self.tokens


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    return db


def make(db, send, *, budget=CAPS):
    return llm.Llm(db, 1, "Asia/Colombo", {"small": SMALL}, budget, send, clock=lambda: NOW)


def ask(model, *, run_id="run-1", messages=None):
    messages = messages or [{"role": "user", "content": "Desk 2 was on overnight."}]
    return model.step(tier="small", agent="housemate", run_id=run_id, prompt_version="v1",
                      system="Look, then decide.", messages=messages, tools=[])  # fmt: skip


def spent(db, usd, at, run_id="old"):
    """A past call that cost `usd`."""
    store.add_llm_call(db, site_id=1, at=at, run_id=run_id, agent="housemate", model="m",
                       prompt_version="v", prompt="", output="", tokens_in=0, tokens_out=0,
                       usd=usd, latency_ms=0)  # fmt: skip


def test_a_step_returns_the_blocks_and_its_cost(db):
    reply = ask(make(db, FakeModel()))
    assert (reply.call_id, reply.blocks, reply.stop_reason) == (1, OK, "end_turn")
    assert reply.usd == pytest.approx(0.002)  # 1000 in at USD 1 a million, 100 out at USD 10


def test_every_step_is_traced_with_the_whole_conversation(db):  # FR-27
    ask(make(db, FakeModel()))
    row = db.execute("""SELECT site_id, run_id, agent, model, prompt_version, prompt, output,
                        tokens_in, tokens_out, usd, ts_utc, latency_ms >= 0 FROM llm_calls""")
    assert row.fetchone() == (
        1, "run-1", "housemate", "small-model", "v1",
        json.dumps({"system": "Look, then decide.",
                    "messages": [{"role": "user", "content": "Desk 2 was on overnight."}]}),
        json.dumps(OK), 1000, 100, pytest.approx(0.002), "2026-11-16T04:00:00+00:00", 1,
    )  # fmt: skip


def test_addresses_never_reach_the_model(db):  # NFR-14
    model = FakeModel()
    said = "Plug at 192.0.2.66, MAC 4C:EB:D6:AA:BB:0C."
    ask(make(db, model), messages=[
        {"role": "user", "content": said},
        {"role": "assistant", "content": [{"type": "text", "text": said}]},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": said}]},
    ])  # fmt: skip
    hidden = "Plug at [ip], MAC [mac]."
    first, second, third = model.calls[0]["messages"]
    assert (first["content"], second["content"][0]["text"], third["content"][0]["content"]) == (
        hidden, hidden, hidden)  # fmt: skip
    assert "192.0.2" not in db.execute("SELECT prompt FROM llm_calls").fetchone()[0]


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ("10.0.0.1", "[ip]"),
        ("4c-eb-d6-aa-bb-0c", "[mac]"),
        ("at 01:30, LKR 3.2, 0.031 kWh", "at 01:30, LKR 3.2, 0.031 kWh"),  # numbers stay
    ],
)
def test_what_counts_as_an_address(text, hidden):
    assert llm.private(text) == hidden


def test_a_failed_step_is_traced_then_raised(db):
    def down(**_):
        raise RuntimeError("no network")

    with pytest.raises(RuntimeError):
        ask(make(db, down))
    row = db.execute("SELECT output, tokens_in, tokens_out, usd FROM llm_calls").fetchone()
    assert row == (None, None, None, None)


def test_the_month_cap_stops_steps_before_they_are_made(db):  # FR-28
    spent(db, 1.0, datetime(2026, 10, 31, 19, 0, tzinfo=UTC))  # 00:30 on 1 Nov in Colombo
    model = FakeModel()
    with pytest.raises(llm.OverBudget, match="month cap USD 1.0 reached"):
        ask(make(db, model))
    assert model.calls == []


def test_last_month_does_not_count(db):
    spent(db, 1.0, datetime(2026, 10, 31, 18, 0, tzinfo=UTC))  # 23:30 on 31 Oct in Colombo
    assert ask(make(db, FakeModel())).blocks


def test_the_run_cap_stops_one_run_only(db):
    model = make(db, FakeModel(), budget=Budget(month_usd=1.0, run_usd=0.002))
    ask(model)
    with pytest.raises(llm.OverBudget, match="run cap USD 0.002 reached"):
        ask(model)
    assert ask(model, run_id="run-2").blocks  # a new run starts at 0


def answer(*content):
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "small-model",
        "content": list(content), "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    })  # fmt: skip


# the real model unreachable: no internet, rate limit, or the service down


def _response(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com"))


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
        anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com")),
        anthropic.RateLimitError("slow down", response=_response(429), body=None),
        anthropic.InternalServerError("down", response=_response(500), body=None),
        anthropic.InternalServerError("overloaded", response=_response(529), body=None),
    ],
)
def test_a_model_out_of_reach_is_unreachable(monkeypatch, error):
    client = anthropic.Anthropic(api_key="test", max_retries=0)

    def create(**_):
        raise error

    monkeypatch.setattr(client.messages, "create", create)
    with pytest.raises(llm.Unreachable):
        llm.claude_step(client)(tier=SMALL, system="S", messages=[], tools=[])


def test_a_bad_request_is_not_unreachable(monkeypatch):  # a bug on our side: it must show
    client = anthropic.Anthropic(api_key="test")
    error = anthropic.BadRequestError("bad", response=_response(400), body=None)

    def create(**_):
        raise error

    monkeypatch.setattr(client.messages, "create", create)
    with pytest.raises(anthropic.BadRequestError):
        llm.claude_step(client)(tier=SMALL, system="S", messages=[], tools=[])


# one step of an agent loop: tools offered, blocks back, traced like any call


def test_the_real_step_sends_tools_with_a_cached_system(monkeypatch):
    client = anthropic.Anthropic(api_key="test")
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        msg = answer({"type": "tool_use", "id": "t1", "name": "power", "input": {"hours": 1}})
        return msg.model_copy(update={"stop_reason": "tool_use"})

    monkeypatch.setattr(client.messages, "create", create)
    tools = [{"name": "power", "description": "d", "input_schema": {"type": "object"}}]
    blocks, stop, tin, tout = llm.claude_step(client)(
        tier=SMALL, system="S", messages=[{"role": "user", "content": "P"}], tools=tools
    )
    assert blocks == [{"type": "tool_use", "id": "t1", "name": "power", "input": {"hours": 1}}]
    assert (stop, tin, tout) == ("tool_use", 1000, 100)
    assert seen["system"] == [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]
    assert seen["tools"] == tools and seen["output_config"] == {"effort": "low"}


def test_the_real_step_streams_words_when_asked(monkeypatch):
    client = anthropic.Anthropic(api_key="test")
    final = answer({"type": "text", "text": "Which room?"})

    class Live:
        text_stream = iter(["Which ", "room?"])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return final

    seen = {}
    monkeypatch.setattr(client.messages, "stream", lambda **kw: seen.update(kw) or Live())
    words = []
    blocks, stop, tin, tout = llm.claude_step(client)(
        tier=SMALL, system="S", messages=[{"role": "user", "content": "P"}], tools=[],
        on_text=words.append,
    )  # fmt: skip
    assert words == ["Which ", "room?"] and blocks == [{"type": "text", "text": "Which room?"}]
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_step_passes_the_stream_on_only_when_asked(db):
    got = []

    def send(*, tier, system, messages, tools, on_text=None):
        got.append(on_text)
        return OK, "end_turn", 10, 1

    m = make(db, send)
    ask(m)
    m.step(tier="small", agent="setup", run_id="r", prompt_version="v", system="S",
           messages=[{"role": "user", "content": "hi"}], tools=[], on_text=print)  # fmt: skip
    assert got == [None, print]
