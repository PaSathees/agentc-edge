# Requirements

Codes like FR-09 or J-04 are explained in the [glossary](glossary.md).

> Version 2, 2026-11-21. Changed with [design.md](design.md) version 2: the AI judges with a house model and tools, and asks the owner while it happens. Changed items say **v2**; dropped items say **dropped**, with the reason. New items are FR-30 to FR-37.

## 1. Problem and context

Small sites (a home now, a plant room later) pay for energy they don't need: devices left on, schedules that no longer match use, no one watching the data. Scope 2 emissions follow the same kWh.

AgentC (MSc prototype) showed an agent can plan device schedules to a monthly kgCO2e goal. As a system it fell short:
- cost: 9 LLM nodes, daily, about USD 0.60 a run, even when nothing changed
- control: plans in Postgres, applied by Node-RED; no human approved anything
- trust: LLMs did the maths, routing and checks; nothing was evaluated
- reach: cloud only (Railway, Postgres, InfluxDB)

Want: the same functions, running on a box inside the site, where code watches all the time, agents wake only when something happens, and a person approves every change before it reaches a device.

- Site v1: my home, 10 Tasmota plugs, logger running since 2026-11-01
- User v1: one owner who sets the rules and approves changes
- Mode: ask and approve; shadow mode first
- v2 finding (audit, 2026-11-17): code-built fixes found 0 proposals in 14 real days and 0 fixes in 3 public homes. Most unusual use is real use at an odd hour. Telling the two apart needs context and judgment, so it moves to the AI
- Evidence: `audit.md` (old system, keep, replace, drop)

## 2. Users and job stories
Format: `J-xx When [situation], I want [action], so I can [outcome].`

User: the site owner, who sets the rules and approves changes. One person in v1.

Setup
- J-01 When I start, I want my current timers and real usage as the first draft, so I don't set up from zero.
- J-02 When I set up a device, I want to say what the agent may do with it and which rules it must keep, so nothing is assumed for me.
- J-03 When a new plug appears or a device moves, I want to be asked about it, so the system stays true to what is plugged in.

Daily
- J-04 (v2) When a device is on and it looks like a mistake, I want to be asked while it is happening, so I can switch it off in one tap or say it's normal.
- J-12 When the system judges what is odd, I want it to know what each device is and how I use it, so it doesn't ask me silly questions.
- J-13 When a timer or plan no longer matches how I live, I want to be told, so my automations don't waste power quietly.
- J-05 When the month is heading over my goal, I want a re-plan, so I still meet it.
- J-06 When I want something changed or explained, I want to ask in chat, so I don't edit timers or read raw data.
- J-07 When the grid or a plug misbehaves, I want to be told, so I know why the data looks odd.

Trust
- J-08 When an agent is new, I want it to propose without writing, so I can trust it before it acts.
- J-09 When I reject a proposal, I want my reason remembered, so I don't see the same bad idea again.
- J-10 When I look back, I want every change, its reason and its cost in one place, so I can check what the system did.
- J-11 When I get a new emission factor document, I want to upload it and approve the extracted value, so reports use the right factor.

## 3. Scope
In (v1):
- One site, one owner; Tasmota smart plugs over HTTP, on the local network
- Runs on one edge box (the hub); only model API calls leave it
- Timer import and usage inference as the first draft
- Device setup: access level, rules (hard or soft), timer slots; discovery of new plugs
- Signals (v2, was the Watcher): left on, unusual use, automation drift, change point, goal drift, plug offline, timer mismatch, health
- House model: device cards from a setup chat, habits from answers, timers with their reason
- Agents on a shared base (v2, ADR-007): housemate (signals), Planner (weekly, goal drift), chat
- One inbox for questions and proposals, in the web UI and as phone notifications; answer, approve, edit, reject with reason; expiry
- Actuator: writes timers after approval, reads them back; shadow mode toggle
- Monthly kgCO2e goal; kWh, LKR and kgCO2e computed in code (GHG Protocol Scope 2, location-based)
- Preferences from rejection reasons; memory page to view and edit what was learned
- Weather (Open-Meteo) as context; hour-of-week profile as the forecast
- History and trace: every change, reason, LLM call and cost
- Evals in CI; shadow mode trial

Later:
- Forecast models with weather features (E2); small local model on the hub (E3); fine-tune (E5)
- MQTT, Modbus and OPC UA adapters (E1)
- Emission factor revision by document upload; hourly carbon intensity, if available for LK
- Voice for chat
- Auto access level (writes without approval) for low-risk devices
- (v2) Load test with 100 simulated plugs (was NFR-15); solver for the Planner (ADR-003 on hold)
- Multiple sites and users; a temperature sensor for comfort

Out:
- Direct on/off control of devices (only timers), except a one-off switch off after the owner's tap (ADR-008)
- Real SCADA or industrial plant data
- Cloud hosting, accounts and billing
- Market-based Scope 2 accounting beyond a renewable source flag
- Compliance claims (ISO 50001, GHG Protocol); terms and methods only

## 4. Functional requirements
Format: `FR-xx [M/S] (J-xx) what it does. AC: acceptance criteria.` M = must, S = should (v1, cut first if late). Thresholds and windows below are defaults, editable in config.

Setup and data
- FR-01 [M] (J-01) Import every plug's timers (`Timer1..16`) as the current schedule. AC: the stored schedule matches each plug's `Timers` output; a plug that doesn't answer is marked and retried next pass.
- FR-02 [M] (J-01) Build a usage profile per plug: median power and on-hours for each hour of the week. AC: built from at least 7 days; rebuilt daily; unit test on a fixture week.
- FR-03 [M] (J-01, J-13) **v2: becomes the automation drift signal (FR-34).** Imported timers are compared with use: a timer that keeps a plug on for hours of standby is raised as a signal, not drafted as a proposal by code.
- FR-04 [M] (J-02) Set up each device: name, room, kind, access level (none, propose, write after approval), allowed timer slots. AC: a device with access none never appears in a question, proposal or action. v2: the device card in FR-30 holds these.
- FR-05 [M] (J-02) Rules per device, hard or soft: must-run window, min runtime per day, latest off, never off; priority low, medium, high. AC: a proposal that breaks a hard rule is blocked and names the rule; a broken soft rule shows as a warning on the card.
- FR-06 [S] (J-03) Discover Tasmota plugs on the subnet; key them by MAC. AC: an unknown MAC starts the setup chat; a known MAC with a new IP updates the IP with an audit row and no question.
- FR-07 [S] (J-03) **Built (Phase 3b2).** Change point: a plug's power when on leaves the whole range of its earlier 2-day windows by 2 times or more (at least 3 earlier windows). AC: one "new device on this plug?" question; none on a normal fixture.
- FR-08 [S] (J-05) Fetch weather daily (Open-Meteo) for the site location. AC: stored hourly; a failed fetch is retried and never blocks a run.

Watching
- FR-09 [M] (J-04) **v2: Signals.** Every 5 min, code raises use signals: left on (power near the device's standby level for 30 min at an hour it is usually off), unusual use (over 2 times usual and at least 10 W more for 30 min, at an hour it is on under 20% of the time). Must-run devices raise neither. AC: the Desk 2 night of 1 to 2 Nov raises 1 left-on signal by 02:10 (on from 01:37, plus 30 minutes, plus one 5-minute pass). **Built (Phase 3b).**; a normal-day fixture raises 0. (Version 1 rule: small loads needed 3 h, so the first alert came at 04:37.)
- FR-10 [M] (J-05) Goal drift event when month-to-date plus projected kgCO2e exceeds the goal. AC: fixture month over goal raises 1 event; under goal raises 0.
- FR-11 [M] (J-07) Plug offline event after 3 failed rounds; timer mismatch event from a daily read-back. AC: fixture with a changed timer raises 1 mismatch event.
- FR-12 [S] (J-07) **Wording built (Phase 2).** Every plug silent 2 minutes in a row: "lost contact with every plug: power cut or the hub's Wi-Fi" (it can't tell which from inside). Shown only, never acted on. AC: fixture raises 1 notice, not 10 offline events.
- FR-13 [M] (J-04, J-05) No repeats: one event per run of unusual use, and one per outage (a plug must answer again before a new one). AC: a 6-hour anomaly raises 1 event, not 24; an outage past midnight raises 1, not 2.

Agents and proposals
- FR-14 [M] (J-04) Proposal contract: device, action (switch off now, timer changes, or a habit), reason, savings (kWh, LKR, kgCO2e), evidence, source, expiry. AC: schema validated before the inbox.
- FR-15 [M] (J-04, J-12) **v2: Housemate agent. Built (Phase 3c): the core in 3c1, in the jobs in 3c2.** For one signal, the agent calls read tools (device card, power segments, usual, house now, automations, habits, past signals, cost) up to 6 times, then ignores, notes, asks, or proposes; at most one question per signal. AC: every number in its text appears in a tool result; nothing for a device with access none; each run within 6 tool calls and USD 0.01.
- FR-16 [S] (J-05) **v2: Planner, Phase 5.** A separate agent on the shared base (ADR-007), started weekly and on goal drift: reads the week's signals, answers and automations; proposes timer changes as one plan, and time-of-use shifts where the tariff has them. AC: on a fixture month over goal, the plan meets the goal or reports the shortfall in kWh. The solver is used only if plans need it (ADR-003 on hold).
- FR-17 [S] (J-06) **v2: Chat, Phase 5.** A separate agent on the shared base (ADR-007), started by a message, with read tools plus draft tools (habit, rule, proposal). AC: no write tools; a request touching an access-none device is refused with the reason.
- FR-18 [M] (J-04) Inbox: questions and proposals, in the local web page and as phone notifications with one button per choice. AC: an expired item can't be answered; the local page works with the internet down.
- FR-19 [M] (J-08) Shadow mode toggle. AC: with shadow mode on, approved proposals are recorded and the Actuator makes 0 writes.

Acting
- FR-20 [M] (J-04) Actuator: on approval, re-check rules and access; write timers only in allowed slots, the "on" timer first, all or nothing; read back; audit row. AC: a failed or mismatched write keeps the old timers, marks the proposal failed and notifies locally; no write outside allowed slots.
- FR-21 [M] (J-10) Audit every proposal, decision, write and read-back, with time, actor and reason. AC: history shows the full chain for any timer on any plug.

Goal and carbon
- FR-22 [M] (J-05) Monthly kgCO2e goal set by the owner; status (on track, at risk, off track) computed in code; a goal change only by proposal. AC: fixture months give on track, at risk and off track at the configured margins; the goal can't be edited outside a proposal.
- FR-23 [M] (J-04, J-05) kWh, LKR and kgCO2e computed in code; each factor stored with its source and date; tariff from config. AC: unit tests against hand-worked values.
- FR-24 **dropped** (v2): baseline from rated W × duty cycle. The setup chat and 7 days of readings are enough for one home.

Learning, history, cost
- FR-25 [M] (J-09) Every answer and rejection is kept: the same question or change is not raised again for that device unless something new happens. AC: after "that's normal on Mondays", a Monday replay raises no question for that device.
- FR-26 [S] (J-09) Habits page: device cards, habits and what was learned; view, edit, delete each. AC: an edit or delete takes effect on the next run and is audited.
- FR-27 [S] (J-10) Trace every LLM call: prompt, output, tokens, USD, latency, linked to its event or chat. AC: every LLM call in a test run has a trace row; monthly USD on the history page equals the sum of rows.
- FR-28 [M] (J-10) Budget caps per run and per month. AC: at the cap, AI signals wait; health notices still go out.

New in v2
- FR-30 [M] (J-02, J-12) **Cards and habits built (Phase 3a); setup chat in 3d.** House model and setup chat: a device card per plug (what is plugged in, kind from a fixed list, room, must run, notes), placements with dates, and a "why" for each timer. The agent asks about 5 questions per plug and the owner confirms before saving. AC: no card is saved without confirmation; a must-run kind stops left-on and unusual signals.
- FR-31 [M] (J-04) **Built (3c2), shown with `agentc questions`; the phone in 3d.** Questions: text plus 2 or 3 choices, each mapped to a known action (switch off now, set a timer, save a habit, remind later). AC: a choice with no known action is blocked by the checks.
- FR-32 [M] (J-04) Switch off now: one `Power Off` after the owner's tap, with the write-time checks (ADR-008). AC: shadow mode sends 0 commands; never sent to a must-run or access-none device.
- FR-33 [M] (J-09) **Taps built (3c2, `agentc answer`); free text with chat (Phase 5).** Habits from answers: a tap becomes a habit row built by code from the question, with its scope shown back (device, days, hours); free text becomes a habit drafted by the agent and confirmed by the owner. Habits are re-checked after 30 days or a change point. AC: a habit stops the matching question in a replay.
- FR-34 [S] (J-13) **Built (Phase 3b); grouped in 3c2: one look per timer a week, once it drifted on 3 of 7 days.** Automation signals: drift (a timer keeps it on, but only standby) and override (a timer turned it off, but it draws power). AC: fixture TV timer on at 06:00 with use from 19:00 raises 1 drift signal.
- FR-35 [M] (J-07) **Built (Phase 2).** Health: no reading for 5 minutes raises "logger stopped"; health notices need no AI and reach the local inbox with the internet down. AC: a stopped logger fixture raises 1 notice.
- FR-36 [M] (J-04) **Built (3c2). Night questions for one device: set its `night_wake_w` lower.** Quiet hours (23:30 to 07:00): at night, ask only above `night_wake_w` (default 50 W) or for devices with night questions on; otherwise a morning question. At most 5 questions a day. AC: the Desk 2 night (5 W) gives 0 questions at night and 1 at 07:00; a 1,500 W fixture asks at once.
- FR-37 [M] (NFR-10) **Built (Phase 2).** A failing AI call never stops other jobs; with the model out of reach (no internet, rate limit, service down) events wait without counting a try; a signal that fails 3 times is parked and flagged. AC: with the model down, the timer check and health notices still run.

Later (not v1)
- FR-29 (J-11) Emission factor revision: document uploaded in chat, vector search, extracted factor as a proposal with page and source.

## 5. Non-functional requirements
Reference machine: HP ProBook G5, 16 GB, i5, CPU only. Reference site: 10 plugs.

Cost
- NFR-01 LLM spend ≤ USD 3 a month at the reference site, enforced by the cap (FR-28).
- NFR-02 A Watcher pass makes 0 LLM calls.

Performance
- NFR-03 A Watcher pass takes ≤ 10 s.
- NFR-04 (v2) A question reaches the owner ≤ 40 min after its condition starts (outside quiet hours).
- NFR-05 (v2) Agent ≤ 30 s per signal; chat first reply ≤ 10 s; Planner ≤ 5 min.
- NFR-06 A timer write is verified by read-back ≤ 30 s after approval.

Resources
- NFR-07 The app (API, agents, UI) uses ≤ 1 GB RAM and < 2% CPU when idle; the logger's minute rounds are never delayed.
- NFR-08 Stored data grows ≤ 50 MB a month at the reference site; readings older than 30 days are downsampled to 15 min.

Reliability
- NFR-09 With the WAN down for 1 h: 100% of logger rounds stored, 0 errors in Watcher, dashboard, inbox and Actuator; agent events queue and all run within 30 min of its return.
- NFR-10 A restart loses 0 events and 0 pending approvals (checkpointer, events in SQLite).

Safety
- NFR-11 100% of numbers in agent text match computed facts.
- NFR-12 0 writes to a device with access none, outside allowed slots, or without an approval (shadow mode: 0 writes at all).

Security and privacy
- NFR-13 Reachable only over Tailscale: 0 ports open to the internet in an outside scan; 100% of API calls without a token rejected.
- NFR-14 0 secrets in git (scan in CI) or prompts; 0 IPs, MACs or locations in model calls (checked on all traces of a test run).

Scale
- NFR-15 **moved to Later** (v2): load test with 100 simulated plugs.

Maintainability
- NFR-16 Python via uv; 0 ruff findings; type hints on public functions; functions ≤ 40 lines unless an exception is noted.
- NFR-17 Every AC of an `[M]` FR has an automated test; core modules (maths, Watcher, rules, Actuator) ≥ 80% line coverage; evals gate CI.
- NFR-18 Changing the model provider changes 1 config value and 0 lines of agent code (`llm()`).

Usability
- NFR-19 Approve or reject from the phone in ≤ 2 taps; the web UI works at phone width, in light and dark.
- NFR-20 A new box goes from fresh Debian to a running system in ≤ 30 min, following the README.

## 6. Success measures

Goals: a system that runs on my site, a portfolio piece, and practice of the process. Measured from traces, the audit table and labelled events; no telemetry.

System
- SM-01 LLM cost a month at least 90% below AgentC on the same inputs (replayed through both).
- SM-02 (v2) Shadow mode, 2 weeks: 40% or fewer false alarms (questions that were my habits), reported with missed waste (nights I mark "should have asked"), and 5 or fewer questions a week.
- SM-03 Event precision at least 80%: of the events raised, the share I label as real.
- SM-04 kWh a month saved by approved changes, against the hour-of-week baseline, reported with its EnPI. No target until shadow mode ends; the number is reported either way.

Portfolio
- SM-05 Demo with approval running: a video of 3 min or less and the README results table filled.
- SM-06 One write-up post linked from the README and the CV.

Process
- SM-07 At release, every FR links to at least one test or eval, and every gate is passed or overridden with a reason.
- SM-08 A retrospective after v1: what to change in the process.

## 7. Constraints and assumptions
Constraints (fixed)
- C-01 Solo. Claude makes the code changes; I own the requirements and design calls, and review each phase in a walkthrough.
- C-02 One hub: ProBook, 16 GB, CPU only, OS and data on a 120 GB SATA SSD, nightly backup to an SD card. The logger already runs on it.
- C-03 No paid infrastructure; the only spend is model API calls (NFR-01).
- C-04 Devices are Tasmota plugs over HTTP: 16 timers each, weekly repeat, local time.
- C-05 Public repo: no AgentC source, thesis results or private addresses.
- C-06 Python via uv; Next.js static export for the UI; Claude models, tiered by cost.

Assumptions (verify; revisit if wrong)
- A-01 Writing a timer doesn't restart the plug or cut its power. Verify on one low-risk plug before FR-20.
- A-02 Plugs keep correct time (NTP) in Asia/Colombo, so timers fire when planned. Verify `Time` and `Timezone` on each.
- A-03 One grid factor for Sri Lanka is a sound v1 method (Scope 2, location-based).
- A-04 (v2) The usual week plus device cards and habits keep false alarms at 40% or less (SM-02).
- A-05 Events stay at a few a week, which keeps cost under the cap (NFR-01).
- A-06 ntfy action buttons can call the API over Tailscale from the phone.
- A-07 CP-SAT solves a week for 10 plugs within NFR-05 on the hub.

## Open questions
The six from version 1 are answered in [design.md](design.md), Settled facts. Choices made in the design review are in design.md, Decisions from review.

- Which grid emission factor for Sri Lanka, from which publisher and year?
- My tariff: block or time-of-use? Shifting load only saves money under time-of-use.
- The monthly kgCO2e goal: what value to start with?
- ntfy: the public ntfy.sh server or self-hosted on the hub? Who can read the messages?
- How do phone approval buttons authenticate to the API?
- Does Electricity Maps cover Sri Lanka with hourly intensity?
