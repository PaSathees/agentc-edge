"""One chat reply loop for the agents the owner talks to (5c review chat, 5d Assistant).

Each agent brings its own prompt, tools, brief and draft parser (a Spec). The loop is the
same: the model reads with tools, each read once a chat; it answers in words, or ends with
`propose`, which the agent's parser turns into a checked draft. Every number it says must
come from the brief, a tool or the owner, and no system words: else it says it again. The
owner's tap on the draft is the only thing that acts, and that is not done here.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentc.agent import system_word
from agentc.checks import made_up
from agentc.llm import Llm, Schema, private

MAX_STEPS = 6  # model calls per owner message; the last one may only propose
MAX_TURNS = 20  # the latest messages the model sees, after the greeting: a long chat stays cheap
LOST = "Sorry, I lost my words there. Could you say that again?"
STUCK = "Sorry, I could not finish that. Could you say it another way?"

Event = Callable[[str, str], None]  # (kind, text): "step", "text" as the reply streams


@dataclass(frozen=True)
class Reply:
    text: str
    draft: Any  # the agent's checked draft, or None
    tools: tuple[str, ...]
    reads: tuple[tuple[str, str], ...] = ()  # (asked, result): new reads to keep


@dataclass
class Spec:
    """One agent's part: what it is told, what it may use, and how its drafts are checked."""

    agent: str  # its name in the trace, e.g. "review"
    version: str  # its prompt version
    system: str
    tier: str  # small or mid
    tools: list[Schema]  # read tools, then propose last
    brief: str  # what code tells it first
    parse: Callable[[dict[str, Any]], Any]  # propose's input -> a draft; ValueError if wrong
    names: Sequence[str]  # device names: never system words
    steps: dict[str, str] = field(default_factory=dict)  # tool -> "looking at ..." for the page
    own: dict[str, Callable[[dict[str, Any]], str]] = field(default_factory=dict)  # code tools
    keep: tuple[str, ...] = ("device", "usual")  # reads kept for the chat's next replies


def reply(spec: Spec, talk: list[tuple[str, str]], *, tools: Any, llm: Llm, run_id: str,  # noqa: ANN401 (any reader)
          on_event: Event | None = None,
          earlier: Sequence[tuple[str, str]] = ()) -> Reply:  # fmt: skip
    """The agent's answer to the owner's last message. talk: (role, text), oldest first,
    starting with the greeting. earlier: what it read before in this chat, (asked, result).
    Raises OverBudget or Unreachable."""
    say = on_event or (lambda kind, text: None)
    facts = [spec.brief, *(text for _, text in talk), *(r for _, r in earlier)]
    messages = _messages(talk, spec.brief, earlier)
    propose = [t for t in spec.tools if t["name"] == "propose"]
    used: list[str] = []
    read = {asked for asked, _ in earlier}
    new: list[tuple[str, str]] = []
    for step in range(MAX_STEPS):
        r = llm.step(tier=spec.tier, agent=spec.agent, run_id=run_id,
                     prompt_version=spec.version, system=spec.system, messages=messages,
                     tools=spec.tools if step < MAX_STEPS - 1 else propose,
                     on_text=lambda words: say("text", words))  # fmt: skip
        messages.append({"role": "assistant", "content": r.blocks})
        text = "".join(b.get("text", "") for b in r.blocks if b.get("type") == "text").strip()
        calls = [b for b in r.blocks if b.get("type") == "tool_use"]
        if not calls:
            if not text or r.stop_reason == "refusal":
                return Reply(LOST, None, tuple(used), tuple(new))
            if not (bad := words(text, spec.names, facts)):
                return Reply(text, None, tuple(used), tuple(new))
            say("step", "checking its numbers")
            messages.append(
                {
                    "role": "user",
                    "content": f"Not shown to the owner: {bad}. Say it"
                    " again with only numbers from the brief, a tool or the owner.",
                }
            )
            continue
        results = []
        for c in calls:
            if c["name"] == "propose":
                try:
                    draft = spec.parse(c["input"])
                    message = str(c["input"]["message"]).strip()
                    if bad := words(message, spec.names, facts):
                        raise ValueError(bad)
                    return Reply(message, draft, tuple(used), tuple(new))
                except (KeyError, TypeError, ValueError) as err:
                    out, error = f"Not shown: {err}. Fix it and call propose again.", True
            else:
                out, error = _look(c, spec, tools, facts, (read, used, new), say)
            results.append({"type": "tool_result", "tool_use_id": c["id"],
                            "content": private(out), "is_error": error})  # fmt: skip
        messages.append({"role": "user", "content": results})
    return Reply(STUCK, None, tuple(used), tuple(new))


def _look(c: dict[str, Any], spec: Spec, tools: Any, facts: list[str],  # noqa: ANN401
          seen: tuple[set[str], list[str], list[tuple[str, str]]], say: Event
          ) -> tuple[str, bool]:  # fmt: skip
    """One tool call: its result, and whether it is an error. Code's own tools (like
    try_change) run each time; a read runs once a chat. seen: what was read in this chat,
    the tools used, and the new reads to keep."""
    read, used, new = seen
    name = c["name"]
    if name in spec.own:
        say("step", spec.steps.get(name, "working it out"))
        out = spec.own[name](c["input"])
        facts.append(out)
        used.append(name)
        return out, out.startswith("Error:")
    if (asked := _asked(name, c["input"])) in read:
        return "You read this already in this chat. Use that result.", False
    read.add(asked)
    say("step", spec.steps.get(name, "looking"))
    out = tools.run(name, c["input"])
    facts.append(out)
    used.append(name)
    error = out.startswith("Error:")
    if not error and name in spec.keep:  # the rest change: read them again next time
        new.append((asked, out))
    return out, error


def words(text: str, names: Sequence[str], facts: list[str]) -> str:
    """What is wrong with words for the owner; "" if nothing."""
    if term := system_word(text, names):
        return f"{term!r} is a system word"
    if missing := made_up(text, "\n".join(facts)):
        return f"numbers not in the brief, a tool or the owner's words: {', '.join(missing)}"
    return ""


def _asked(name: str, args: dict[str, Any]) -> str:
    return f"{name} {json.dumps(args, sort_keys=True)}"


def _messages(talk: list[tuple[str, str]], first: str, earlier: Sequence[tuple[str, str]]
              ) -> list[dict[str, Any]]:  # fmt: skip
    """The chat as model messages. The first holds the brief, what was read before in this
    chat, and the greeting, which code wrote."""
    head = f"{first}\n\nYou opened the chat with: {talk[0][1]}"
    if earlier:
        head += "\n\nWhat you read earlier in this chat (still true):\n" + "\n\n".join(
            f"{asked}:\n{result}" for asked, result in earlier)  # fmt: skip
    messages: list[dict[str, Any]] = []
    later = talk[1:][-MAX_TURNS:]
    if later and later[0][0] != "owner":  # the model's first message is always the owner's
        later = later[1:]
    for role, text in later:
        kind = "user" if role == "owner" else "assistant"
        if not messages:
            text = f"{private(head)}\n\nThe owner says: {text}"
        messages.append({"role": kind, "content": text})
    return messages
