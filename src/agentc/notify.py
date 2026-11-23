"""Messages to the owner's phone through ntfy, a push service on the hub (FR-31, 3d).

A message has at most 3 buttons. A button makes the phone POST to the hub over Tailscale,
with a token signed by AGENTC_SECRET, so only a real button (or the inbox) can answer.
Code builds every message; no AI here.
"""

import hashlib
import hmac
import json
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from agentc.config import Owner
from agentc.owner import Question

MAX_BUTTONS = 3  # ntfy's limit
NIGHT = {"switch_off_now": "Switch all off", "save_habit": "All normal"}  # one tap for the night


class NotSent(Exception):
    """ntfy did not take the message. It stays unsent and goes on the next pass."""


@dataclass(frozen=True)
class Button:
    label: str
    url: str
    post: bool = True  # True: the phone POSTs the url (a tap); False: it opens it (a link)


@dataclass(frozen=True)
class Message:
    title: str
    text: str
    buttons: tuple[Button, ...] = ()
    quiet: bool = False  # no sound: a summary, not a question


class Publish(Protocol):
    def __call__(self, m: Message, /) -> None: ...


def token(secret: str, *parts: object) -> str:
    """A short signature for a button: only the hub, which knows the secret, can make one."""
    text = "/".join(str(p) for p in parts)
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()[:20]


def valid(secret: str, given: str, *parts: object) -> bool:
    return hmac.compare_digest(token(secret, *parts), given)


def answer_url(hub: str, secret: str, question_id: int, choice: int) -> str:
    return f"{hub}/answer/{question_id}/{choice}/{token(secret, question_id, choice)}"


def night_url(hub: str, secret: str, night: date, does: str) -> str:
    return f"{hub}/night/{night}/{does}/{token(secret, night, does)}"


def single(q: Question, device: str, *, hub: str, secret: str) -> Message:
    """One question, with its own choices as buttons."""
    buttons = tuple(Button(c.label, answer_url(hub, secret, q.id, n))
                    for n, c in enumerate(q.decision.choices, 1))  # fmt: skip
    return Message(device, q.decision.text, buttons[:MAX_BUTTONS])


def night(
    items: Sequence[tuple[Question, str, float]], *, day: date, hub: str, secret: str
) -> Message:
    """The night's questions in one message (FM-07): one line a device, one tap for all.

    items: (question, device, watts now). "Switch all off" and "All normal" act on every
    question that offers them; the inbox has each question on its own.
    """
    lines = [f"{device}: about {watts:.0f} W now" for _, device, watts in items]
    offered = {c.does for q, _, _ in items for c in q.decision.choices}
    buttons = [Button(label, night_url(hub, secret, day, does))
               for does, label in NIGHT.items() if does in offered]  # fmt: skip
    buttons.append(Button("Open inbox", f"{hub}/", post=False))
    n = len(items)
    return Message(f"{n} devices stayed on last night", "\n".join(lines), tuple(buttons))


def ping(*, hub: str, secret: str, at: str) -> Message:
    """A test message for setting up the phone: a link, and a tap the hub answers."""
    return Message("AgentC test", f"Sent {at}. Tap the button: the hub should answer.",
                   (Button("Tap test", f"{hub}/ping/{token(secret, 'ping')}"),
                    Button("Open inbox", f"{hub}/", post=False)))  # fmt: skip


def summary(notes: Sequence[str]) -> Message:
    """The day's notes in one quiet message."""
    n = len(notes)
    return Message(f"Today: {n} note{'s' if n > 1 else ''}", "\n".join(f"- {x}" for x in notes),
                   quiet=True)  # fmt: skip


def ntfy(owner: Owner, *, timeout_s: float = 10.0) -> Publish:
    """Publish to the owner's ntfy server as JSON (ntfy's publish-as-JSON form)."""

    def publish(m: Message) -> None:
        actions = [
            {"action": "http", "label": b.label, "url": b.url, "method": "POST", "clear": True}
            if b.post
            else {"action": "view", "label": b.label, "url": b.url}
            for b in m.buttons
        ]
        body = {"topic": owner.topic, "title": m.title, "message": m.text,
                "priority": 2 if m.quiet else 3, "actions": actions}  # fmt: skip
        req = urllib.request.Request(owner.ntfy_url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                resp.read()
        except OSError as err:
            raise NotSent(f"ntfy at {owner.ntfy_url}: {err!r}") from err

    return publish
