from datetime import UTC, datetime

import anthropic
import httpx2 as httpx  # the HTTP library the anthropic SDK uses
import pytest
from anthropic.types import Message

from agentc import llm, store
from agentc.config import Budget, Tier

NOW = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
SMALL = Tier("small-model", usd_in=1.0, usd_out=10.0, effort="low")
SCHEMA = {"type": "object"}
CAPS = Budget(month_usd=1.0, run_usd=0.05)


class FakeModel:
    """Stands in for the provider: notes each call, answers with fixed text and tokens."""

    def __init__(self, text='{"option": "A"}', tokens=(1000, 100)):
        self.text, self.tokens, self.calls = text, tokens, []

    def __call__(self, *, tier, system, prompt, schema):
        self.calls.append({"model": tier.model, "system": system, "prompt": prompt})
        return self.text, *self.tokens


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    return db


def make(db, send, *, budget=CAPS):
    return llm.Llm(db, 1, "Asia/Colombo", {"small": SMALL}, budget, send, clock=lambda: NOW)


def ask(model, *, run_id="run-1", prompt="Desk 2 was on overnight."):
    return model(tier="small", agent="responder", run_id=run_id, prompt_version="responder-v1",
                 system="Pick one option.", prompt=prompt, schema=SCHEMA)  # fmt: skip


def spent(db, usd, at, run_id="old"):
    """A past call that cost `usd`."""
    store.add_llm_call(db, site_id=1, at=at, run_id=run_id, agent="responder", model="m",
                       prompt_version="v", prompt="", output="", tokens_in=0, tokens_out=0,
                       usd=usd, latency_ms=0)  # fmt: skip


def test_a_call_returns_the_answer_and_its_cost(db):
    reply = ask(make(db, FakeModel()))
    assert (reply.call_id, reply.text) == (1, '{"option": "A"}')
    assert reply.usd == pytest.approx(0.002)  # 1000 in at USD 1 a million, 100 out at USD 10


def test_every_call_is_traced(db):  # FR-27
    ask(make(db, FakeModel()))
    row = db.execute("""SELECT site_id, run_id, agent, model, prompt_version, prompt, output,
                        tokens_in, tokens_out, usd, ts_utc, latency_ms >= 0 FROM llm_calls""")
    assert row.fetchone() == (
        1, "run-1", "responder", "small-model", "responder-v1", "Desk 2 was on overnight.",
        '{"option": "A"}', 1000, 100, pytest.approx(0.002), "2026-11-16T04:00:00+00:00", 1,
    )  # fmt: skip


def test_addresses_never_reach_the_model(db):  # NFR-14
    model = FakeModel()
    ask(make(db, model), prompt="Plug at 192.0.2.66, MAC 4C:EB:D6:AA:BB:0C.")
    assert model.calls[0]["prompt"] == "Plug at [ip], MAC [mac]."
    assert db.execute("SELECT prompt FROM llm_calls").fetchone() == ("Plug at [ip], MAC [mac].",)


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


def test_a_failed_call_is_traced_then_raised(db):
    def down(**_):
        raise RuntimeError("no network")

    with pytest.raises(RuntimeError):
        ask(make(db, down))
    row = db.execute("SELECT output, tokens_in, tokens_out, usd FROM llm_calls").fetchone()
    assert row == (None, None, None, None)


def test_the_month_cap_stops_calls_before_they_are_made(db):  # FR-28
    spent(db, 1.0, datetime(2026, 10, 31, 19, 0, tzinfo=UTC))  # 00:30 on 1 Nov in Colombo
    model = FakeModel()
    with pytest.raises(llm.OverBudget, match="month cap USD 1.0 reached"):
        ask(make(db, model))
    assert model.calls == []


def test_last_month_does_not_count(db):
    spent(db, 1.0, datetime(2026, 10, 31, 18, 0, tzinfo=UTC))  # 23:30 on 31 Oct in Colombo
    assert ask(make(db, FakeModel())).text


def test_the_run_cap_stops_one_run_only(db):
    model = make(db, FakeModel(), budget=Budget(month_usd=1.0, run_usd=0.002))
    ask(model)
    with pytest.raises(llm.OverBudget, match="run cap USD 0.002 reached"):
        ask(model)
    assert ask(model, run_id="run-2").text  # a new run starts at 0


def answer(*content):
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "small-model",
        "content": list(content), "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    })  # fmt: skip


def test_the_real_model_must_answer_in_the_schema(monkeypatch):
    client = anthropic.Anthropic(api_key="test")
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return answer({"type": "thinking", "thinking": "", "signature": "s"},
                      {"type": "text", "text": '{"option": "A"}'})  # fmt: skip

    monkeypatch.setattr(client.messages, "create", create)
    send = llm.claude(client)
    assert send(tier=SMALL, system="S", prompt="P", schema=SCHEMA) == ('{"option": "A"}', 1000, 100)
    assert seen == {
        "model": "small-model", "max_tokens": 4000, "system": "S",
        "messages": [{"role": "user", "content": "P"}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    }  # fmt: skip


def test_no_text_in_the_answer_is_empty(monkeypatch):
    client = anthropic.Anthropic(api_key="test")
    monkeypatch.setattr(client.messages, "create", lambda **_: answer())
    assert llm.claude(client)(tier=SMALL, system="S", prompt="P", schema=SCHEMA)[0] == ""


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
        llm.claude(client)(tier=SMALL, system="S", prompt="P", schema=SCHEMA)


def test_a_bad_request_is_not_unreachable(monkeypatch):  # a bug on our side: it must show
    client = anthropic.Anthropic(api_key="test")
    error = anthropic.BadRequestError("bad", response=_response(400), body=None)

    def create(**_):
        raise error

    monkeypatch.setattr(client.messages, "create", create)
    with pytest.raises(anthropic.BadRequestError):
        llm.claude(client)(tier=SMALL, system="S", prompt="P", schema=SCHEMA)


# one step of an agent loop: tools offered, blocks back, traced like any call


def test_a_step_is_traced_and_priced(db):
    def step(*, tier, system, messages, tools):
        return [{"type": "text", "text": "ok"}], "end_turn", 1000, 100

    m = llm.Llm(db, 1, "Asia/Colombo", {"small": SMALL}, CAPS, send=None, step_send=step,
                clock=lambda: NOW)  # fmt: skip
    reply = m.step(tier="small", agent="housemate", run_id="run-1", prompt_version="v",
                   system="S at 192.0.2.5", messages=[{"role": "user", "content": "hi"}],
                   tools=[])  # fmt: skip
    assert (reply.blocks, reply.stop_reason, reply.usd) == ([{"type": "text", "text": "ok"}],
                                                            "end_turn", 0.002)  # fmt: skip
    (prompt,) = db.execute("SELECT prompt FROM llm_calls").fetchone()
    assert "[ip]" in prompt and "192.0.2" not in prompt


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


def test_the_real_step_out_of_reach_is_unreachable(monkeypatch):
    client = anthropic.Anthropic(api_key="test", max_retries=0)

    def create(**_):
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com")
        )

    monkeypatch.setattr(client.messages, "create", create)
    with pytest.raises(llm.Unreachable):
        llm.claude_step(client)(tier=SMALL, system="S", messages=[], tools=[])
