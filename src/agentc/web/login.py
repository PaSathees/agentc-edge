"""Who may use the web pages, and on which addresses (Phase 4: before a tap can switch a plug).

Two ways in; either one is enough:
- Tailscale. Tailscale Serve runs on the hub and tells the app who is asking, in the
  Tailscale-User-Login header. The header is trusted only on a request from the hub itself,
  so nobody on the home network can fake it. AGENTC_TAILSCALE_USER says who may enter.
- Password. AGENTC_PASSWORD in .env. The login page sets a signed cookie for 30 days.

With neither set (a first start) the pages stay open, and shadow mode cannot be turned off,
so nothing is ever written to a plug. The phone's buttons need no login: each one carries
its own signed, single-use token.

The host check blocks DNS rebinding: a web page that renames itself to the hub's address so
a browser on the home network talks to the hub for it. Only the hub's own names and plain
IP addresses are answered.
"""

import hmac
import ipaddress
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from agentc import notify
from agentc.web.deps import Now, Secret, pages

COOKIE = "agentc_login"
DAYS = 30  # a login lasts this long on one browser
TAILSCALE_HEADER = "tailscale-user-login"
LOCAL = ("127.0.0.1", "::1")  # Tailscale Serve reaches the app from the hub itself
NAMES = ("localhost", "testserver")  # testserver: the name FastAPI's test client uses

router = APIRouter()


@dataclass(frozen=True)
class Login:
    password: str = ""
    tailscale: frozenset[str] = frozenset()

    @property
    def set(self) -> bool:
        """Is any way in configured? Without one, the pages are open and nothing is written."""
        return bool(self.password or self.tailscale)


def login() -> Login:
    """The ways in, from .env."""
    users = os.environ.get("AGENTC_TAILSCALE_USER", "")
    return Login(os.environ.get("AGENTC_PASSWORD", ""),
                 frozenset(u.strip().lower() for u in users.split(",") if u.strip()))  # fmt: skip


Ways = Annotated[Login, Depends(login)]


def owner_only(request: Request, ways: Ways, key: Secret, now: Now) -> None:
    """For every page but the phone's buttons. A page asks for a login; a form or a
    stream gets 401, as it cannot show the login page."""
    if not ways.set or is_owner(request, ways, key, now):
        return
    if request.method == "GET" and "hx-request" not in request.headers:
        back = quote(request.url.path, safe="/")
        raise HTTPException(303, headers={"Location": f"/login?next={back}"})
    raise HTTPException(401, "Log in again: open the page and sign in.")


def is_owner(request: Request, ways: Ways, key: str, now: datetime) -> bool:
    user = request.headers.get(TAILSCALE_HEADER, "").lower()
    from_hub = request.client is not None and request.client.host in LOCAL
    if user and from_hub and user in ways.tailscale:
        return True
    return bool(ways.password) and _valid(request.cookies.get(COOKIE, ""), key, now)


Owner = Depends(owner_only)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, ways: Ways, next: str = "/") -> HTMLResponse:  # noqa: A002
    return _page(request, ways, _safe(next), "")


@router.post("/login", response_class=HTMLResponse)
def sign_in(request: Request, ways: Ways, key: Secret, now: Now,
            password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"
            ) -> Response:  # fmt: skip  # noqa: A002
    if not ways.password or not hmac.compare_digest(password.encode(), ways.password.encode()):
        time.sleep(1)  # a guesser gets one try a second
        return _page(request, ways, _safe(next), "That is not the password.", status=401)
    done = RedirectResponse(_safe(next), status_code=303)
    until = int((now + timedelta(days=DAYS)).timestamp())
    done.set_cookie(COOKIE, f"{until}.{notify.token(key, 'login', until)}", max_age=DAYS * 86400,
                    httponly=True, samesite="lax", secure=_https(request))  # fmt: skip
    return done


def _page(request: Request, ways: Login, next: str, error: str, status: int = 200  # noqa: A002
          ) -> HTMLResponse:  # fmt: skip
    return pages.TemplateResponse(request, "login.html", {
        "title": "Sign in", "page": "", "next": next, "error": error,
        "password": bool(ways.password)}, status_code=status)  # fmt: skip


def _valid(cookie: str, key: str, now: datetime) -> bool:
    until, _, tok = cookie.partition(".")
    return (
        until.isdigit() and int(until) > now.timestamp() and notify.valid(key, tok, "login", until)
    )


def _safe(path: str) -> str:
    """Go back only to a page on this hub: "/setup", never "//evil.example" or "https://..."."""
    return path if path.startswith("/") and not path.startswith(("//", "/\\")) else "/"


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def allowed_host(host: str) -> bool:
    """A plain IP address, localhost, the hub's name from AGENTC_HUB, or one in AGENTC_HOSTS."""
    name = urlsplit(f"//{host}").hostname or ""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    hub = urlsplit(os.environ.get("AGENTC_HUB", "")).hostname or ""
    extra = os.environ.get("AGENTC_HOSTS", "").split(",")
    return name in {*NAMES, hub.lower(), *(h.strip().lower() for h in extra)} - {""}


class HostCheck:
    """Answers only requests made for one of the hub's own addresses (see allowed_host)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not allowed_host(Headers(scope=scope).get("host", "")):
            await PlainTextResponse("Unknown host name. Add it to AGENTC_HOSTS in .env.", 400)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)
