"""One door for every model call (decision ADR-005).

It picks the model for a tier, hides private addresses, keeps spend under the caps,
and saves a trace of each call. Agents never call a provider directly.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import anthropic

from agentc import store
from agentc.config import Budget, Tier

# no connection or timeout, 429 too many requests, 5xx the service is down or overloaded
OUT_OF_REACH = (anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.InternalServerError)  # fmt: skip
MAX_TOKENS = 4000  # thinking counts too; a pick and a 2-sentence reason need far less
IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
Schema = dict[str, Any]  # JSON schema the answer must match
Message = dict[str, Any]  # one turn: {"role": ..., "content": str or a list of blocks}
Block = dict[str, Any]  # one content block: text, thinking, tool_use or tool_result
MAX_STEP_TOKENS = 8000  # one step of the agent loop: a few tool calls or a decision


class Send(Protocol):
    """Ask the model; return the answer text, tokens in and tokens out."""

    def __call__(self, *, tier: Tier, system: str, prompt: str, schema: Schema
                 ) -> tuple[str, int, int]: ...  # fmt: skip


class StepSend(Protocol):
    """One step of an agent loop: messages and tools in; content blocks, why it stopped,
    tokens in and tokens out back. Blocks are plain dicts, passed back unchanged."""

    def __call__(self, *, tier: Tier, system: str, messages: list[Message], tools: list[Schema]
                 ) -> tuple[list[Block], str, int, int]: ...  # fmt: skip


class OverBudget(Exception):
    """The month or the run reached its cap: no more model calls (FR-28)."""


class Unreachable(Exception):
    """The model can't be reached now: no internet, rate limit, or the service is down.

    Not a fault of the event, so callers stop and let events wait (FR-37). Other errors,
    like a bad request, are bugs on our side and are raised as they are.
    """


@dataclass(frozen=True)
class StepReply:
    call_id: int  # the trace row
    blocks: list[Block]
    stop_reason: str
    usd: float


@dataclass(frozen=True)
class Reply:
    call_id: int  # the trace row
    text: str
    usd: float


def private(text: str) -> str:
    """Hide IP and MAC addresses: they never reach a model (NFR-14)."""
    return MAC.sub("[mac]", IP.sub("[ip]", text))


@dataclass
class Llm:
    db: Connection
    site_id: int
    tz: str
    tiers: dict[str, Tier]
    budget: Budget
    send: Send | None  # one call with a JSON answer (the v1 Responder)
    step_send: StepSend | None = None  # one step of an agent loop with tools
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __call__(
        self, *, tier: str, agent: str, run_id: str, prompt_version: str, system: str,
        prompt: str, schema: Schema,
    ) -> Reply:  # fmt: skip
        """Call the model and trace it. Raises OverBudget before the call if a cap is reached."""
        t, now = self.tiers[tier], self.clock()
        self._check_budget(run_id, now)
        system, prompt = private(system), private(prompt)
        trace = {"site_id": self.site_id, "at": now, "run_id": run_id, "agent": agent,
                 "model": t.model, "prompt_version": prompt_version, "prompt": prompt}  # fmt: skip
        start = time.perf_counter()
        assert self.send is not None, "no model for single calls"
        try:
            text, tokens_in, tokens_out = self.send(tier=t, system=system, prompt=prompt,
                                                    schema=schema)  # fmt: skip
        except Exception:
            store.add_llm_call(self.db, **trace, output=None, tokens_in=None, tokens_out=None,
                               usd=None, latency_ms=_ms(start))  # fmt: skip
            raise
        usd = (tokens_in * t.usd_in + tokens_out * t.usd_out) / 1e6
        cid = store.add_llm_call(self.db, **trace, output=text, tokens_in=tokens_in,
                                 tokens_out=tokens_out, usd=usd, latency_ms=_ms(start))  # fmt: skip
        return Reply(cid, text, usd)

    def step(
        self, *, tier: str, agent: str, run_id: str, prompt_version: str, system: str,
        messages: list[Message], tools: list[Schema],
    ) -> StepReply:  # fmt: skip
        """One step of an agent loop, traced and capped like any call (ADR-009).

        The trace keeps the system prompt and the whole conversation so far, so a run can be
        read back step by step.
        """
        t, now = self.tiers[tier], self.clock()
        self._check_budget(run_id, now)
        assert self.step_send is not None, "no model for agent steps"
        system, messages = private(system), [_private_message(m) for m in messages]
        trace = {"site_id": self.site_id, "at": now, "run_id": run_id, "agent": agent,
                 "model": t.model, "prompt_version": prompt_version,
                 "prompt": json.dumps({"system": system, "messages": messages})}  # fmt: skip
        start = time.perf_counter()
        try:
            blocks, stop, tokens_in, tokens_out = self.step_send(
                tier=t, system=system, messages=messages, tools=tools
            )
        except Exception:
            store.add_llm_call(self.db, **trace, output=None, tokens_in=None, tokens_out=None,
                               usd=None, latency_ms=_ms(start))  # fmt: skip
            raise
        usd = (tokens_in * t.usd_in + tokens_out * t.usd_out) / 1e6
        cid = store.add_llm_call(self.db, **trace, output=json.dumps(blocks), tokens_in=tokens_in,
                                 tokens_out=tokens_out, usd=usd, latency_ms=_ms(start))  # fmt: skip
        return StepReply(cid, blocks, stop, usd)

    def _check_budget(self, run_id: str, now: datetime) -> None:
        local = now.astimezone(ZoneInfo(self.tz))
        month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if store.llm_usd(self.db, site_id=self.site_id, since=month) >= self.budget.month_usd:
            raise OverBudget(f"month cap USD {self.budget.month_usd} reached")
        if store.llm_usd(self.db, site_id=self.site_id, run_id=run_id) >= self.budget.run_usd:
            raise OverBudget(f"run cap USD {self.budget.run_usd} reached")


def _private_message(m: Message) -> Message:
    """A message with IP and MAC addresses hidden in every piece of text."""
    if isinstance(m["content"], str):
        return {**m, "content": private(m["content"])}
    blocks = []
    for b in m["content"]:
        if isinstance(b.get("content"), str):  # a tool_result
            b = {**b, "content": private(b["content"])}
        elif b.get("type") == "text":
            b = {**b, "text": private(b["text"])}
        blocks.append(b)
    return {**m, "content": blocks}


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def claude(client: anthropic.Anthropic) -> Send:
    """The real model, through the Anthropic SDK. The key comes from ANTHROPIC_API_KEY."""

    def send(*, tier: Tier, system: str, prompt: str, schema: Schema) -> tuple[str, int, int]:
        try:
            msg = client.messages.create(
                model=tier.model, max_tokens=MAX_TOKENS, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": tier.effort,
                               "format": {"type": "json_schema", "schema": schema}},
            )  # fmt: skip
        except OUT_OF_REACH as err:
            raise Unreachable(f"{type(err).__name__}: {err}") from err
        text = next((b.text for b in msg.content if b.type == "text"), "")
        return text, msg.usage.input_tokens, msg.usage.output_tokens

    return send


def claude_step(client: anthropic.Anthropic) -> StepSend:
    """One agent step on the real model. The system prompt and tools are the same on every
    step and every run, so they are cached; thinking blocks come back and are passed back."""

    def step(*, tier: Tier, system: str, messages: list[Message], tools: list[Schema]
             ) -> tuple[list[Block], str, int, int]:  # fmt: skip
        try:
            msg = client.messages.create(
                model=tier.model, max_tokens=MAX_STEP_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools, messages=messages,  # pyright: ignore[reportArgumentType]
                output_config={"effort": tier.effort},
            )  # fmt: skip
        except OUT_OF_REACH as err:
            raise Unreachable(f"{type(err).__name__}: {err}") from err
        blocks = [b.model_dump(mode="json", exclude_none=True) for b in msg.content]
        u = msg.usage
        tokens_in = (
            u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
        )  # counted at the full price: the cap stays safe
        return blocks, msg.stop_reason or "end_turn", tokens_in, u.output_tokens

    return step
