# Changelog

One line per phase. Details: `docs/` and the pull requests.

## Unreleased

- **Phase 5b1 to 5b2, 2026-11-25:** the weekly Planner. `what_if` replays 14 days of real readings to price a timer change; `options` finds each timer's furthest move that never cut real use; the Planner proposes at most 3 changes, each checked by code, ready to do only if it never cut real use. `agentc plan` runs it now. The housemate's and the Planner's words are checked for system terms.
- **Phase 5a, 2026-11-25:** four agents, four jobs (ADR-010): the housemate now only ignores, notes or asks about waste happening now; no proposals and no timers, which become the weekly Planner's; notes are one sentence. Also: the Devices page shows each plug's power and timers, with an on/off switch for devices set to write, and `scripts/dev.py` runs the web app locally.
- **Phase 4, 2026-11-24:** the first writes to plugs, only after your tap. A login for the pages (Tailscale or a password) and a host check. The Actuator, the only code that changes a plug: switch off now with undo, and timer pairs written all or nothing in slots 9 to 16, each read back; at most 4 changes a plug a day; shadow mode on by default, turned off on the Devices page. The setup chat keeps what it read between messages and suggests habits from the usual week (FM-08).
- **Phase 3d3b, 2026-11-24:** the setup chat works on a phone. It opens at the newest message and nothing hides under the message box. The mic keeps listening through pauses and never sends by itself: tap stop, check the words, then Send. Read aloud works on iPhone (turn it on with the speaker button). Start again moved to the menu.
- **Phase 3d3, 2026-11-24:** a web UI (HTMX and Jinja, our own CSS) with a Devices page and a setup chat: the agent reads the plug, asks a few questions and drafts a card; only your Save writes it. Replies stream in; voice in and out where the browser allows it.
- **Phase 3d1 to 3d2, 2026-11-23:** questions on your phone through ntfy, with signed tap buttons and one message for the night; an inbox page; the whole system as one Docker Compose stack, set up in 6 steps (`docs/setup.md`); restart-safe jobs.
- **Phase 3c, 2026-11-23:** the housemate agent replaces version 1's Responder: it looks with read-only tools, then ignores, notes, asks or proposes; code checks every decision.
- **Phase 3a to 3b, 2026-11-22:** a house model (device cards, habits, the plugs' own timers and rules); new signals: left on, timer drift, override, a new device on a plug.
- **Phase 2, 2026-11-21:** a logger heartbeat, honest "lost contact" alerts, and AI errors that never stop other jobs.
- **Phase 1, 2026-11-21:** design version 2, after a review of version 1.
