"""What every page needs, as FastAPI dependencies: the config, the secret, app.db for one
request, the clock and the model. Tests swap them with `app.dependency_overrides`."""

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated

import anthropic
from fastapi import Depends, Form, HTTPException
from fastapi.templating import Jinja2Templates

from agentc import config, house, llm, notify, store
from agentc.config import Config
from agentc.devices.base import Plug, days_words
from agentc.devices.tasmota import TasmotaPlug
from agentc.llm import StepSend
from agentc.notify import Publish

WEB = Path(__file__).parent
pages = Jinja2Templates(directory=WEB / "templates")  # escapes every value it prints
pages.env.filters["days"] = days_words  # "0111110" -> "Monday to Friday"
pages.env.filters["runs_always"] = house.runs_always  # the owner's word, else its kind


def settings() -> Config:
    """config.toml, from AGENTC_CONFIG (default: the working folder's)."""
    return config.load(os.environ.get("AGENTC_CONFIG", "config.toml"))


def secret(cfg: Annotated[Config, Depends(settings)]) -> str:
    """The key that signs the buttons: AGENTC_SECRET, else the data folder's own."""
    return config.secret(cfg)


def publisher(cfg: Annotated[Config, Depends(settings)],
              key: Annotated[str, Depends(secret)]) -> Publish | None:  # fmt: skip
    """ntfy, to say back what a tap did. None without AGENTC_HUB."""
    return notify.ntfy(cfg.owner, key) if cfg.owner else None


def database(cfg: Annotated[Config, Depends(settings)]) -> Iterator[tuple[Connection, int]]:
    """app.db for one request, and the site's id; closed after. The jobs make the tables."""
    db = store.open_app(cfg.app_db)
    try:
        if (site := store.site_id(db, name=cfg.site)) is None:
            raise HTTPException(503, "Starting: no data yet. Try again in a minute.")
        yield db, site
    finally:
        db.close()


def clock() -> datetime:
    return datetime.now(UTC)


def stepper() -> StepSend | None:
    """The model, for the setup chat. None without ANTHROPIC_API_KEY."""
    return llm.claude_step(anthropic.Anthropic()) if os.environ.get("ANTHROPIC_API_KEY") else None


def connect() -> Callable[[str], Plug]:
    """How the pages reach a plug: over HTTP, with a short wait, so one plug that is off the
    network cannot hold the page for long."""
    return lambda ip: TasmotaPlug(ip, timeout_s=2.0)


def known(card: house.Card) -> bool:
    """Has the owner said anything about this device yet?"""
    return card != house.Card()


Cfg = Annotated[Config, Depends(settings)]
Secret = Annotated[str, Depends(secret)]
Now = Annotated[datetime, Depends(clock)]
Say = Annotated[Publish | None, Depends(publisher)]
Db = Annotated[tuple[Connection, int], Depends(database)]
Model = Annotated[StepSend | None, Depends(stepper)]
Tok = Annotated[str, Form()]
Connect = Annotated[Callable[[str], Plug], Depends(connect)]
