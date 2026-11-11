# Requirements

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
- Mode: propose and approve; shadow mode first
- Evidence: `audit.md` (old system, keep, replace, drop)

## 2. Users and job stories
Format: `J-xx When [situation], I want [action], so I can [outcome].`

User: the site owner, who sets the rules and approves changes. One person in v1.

Setup
- J-01 When I start, I want my current timers and real usage as the first draft, so I don't set up from zero.
- J-02 When I set up a device, I want to say what the agent may do with it and which rules it must keep, so nothing is assumed for me.
- J-03 When a new plug appears or a device moves, I want to be asked about it, so the system stays true to what is plugged in.

Daily
- J-04 When a device uses more than usual, I want a fix proposed with its savings, so I can approve it in one tap.
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
- Watcher: anomaly, goal drift, plug offline, timer mismatch, grid events
- Agents: Responder (events), Planner (weekly, goal drift; solver, simulation), Assistant (chat)
- One proposal inbox, in the web UI and as phone notifications; approve, edit, reject with reason; expiry
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
- Voice for the Assistant
- Auto access level (writes without approval) for low-risk devices
- Multiple sites and users; a temperature sensor for comfort

Out:
- Direct on/off control of devices (only timers)
- Real SCADA or industrial plant data
- Cloud hosting, accounts and billing
- Market-based Scope 2 accounting beyond a renewable source flag
- Compliance claims (ISO 50001, GHG Protocol); terms and methods only

## 4. Functional requirements
Format: `FR-xx [M/S] (J-xx) what it does. AC: acceptance criteria.` M = must, S = should (v1, cut first if late). Thresholds and windows below are defaults, editable in config.

Setup and data
- FR-01 [M] (J-01) Import every plug's timers (`Timer1..16`) as the current schedule. AC: the stored schedule matches each plug's `Timers` output; a plug that doesn't answer is marked and retried next pass.
- FR-02 [M] (J-01) Build a usage profile per plug: median power and on-hours for each hour of the week. AC: built from at least 7 days; rebuilt daily; unit test on a fixture week.
- FR-03 [M] (J-01) Draft the first proposals where imported timers and the profile differ by 1 h a day or more. AC: fixture with timer on 18:00 to 23:00 and use 18:00 to 21:00 gives one proposal, "off at 21:00".
- FR-04 [M] (J-02) Set up each device: name, room, type, access level (none, propose, write after approval), allowed timer slots. AC: a device with access none never appears in a proposal.
- FR-05 [M] (J-02) Rules per device, hard or soft: must-run window, min runtime per day, latest off, never off; priority low, medium, high. AC: a proposal that breaks a hard rule is blocked and names the rule; a broken soft rule shows as a warning on the card.
- FR-06 [S] (J-03) Discover Tasmota plugs on the subnet; key them by MAC. AC: an unknown MAC creates a setup proposal; a known MAC with a new IP updates the IP with an audit row and no proposal.
- FR-07 [S] (J-03) Detect a device change: a plug's power signature differs from its profile for 2 days. AC: one "new device on this plug?" setup proposal; none on a normal fixture.
- FR-08 [S] (J-05) Fetch weather daily (Open-Meteo) for the site location. AC: stored hourly; a failed fetch is retried and never blocks a run.

Watching
- FR-09 [M] (J-04) Watcher runs every 15 min and raises an anomaly when power stays above the profile by the plug's threshold for 30 min or more. AC: replayed "charger left on" fixture raises 1 event; a normal-day fixture raises 0.
- FR-10 [M] (J-05) Goal drift event when month-to-date plus projected kgCO2e exceeds the goal. AC: fixture month over goal raises 1 event; under goal raises 0.
- FR-11 [M] (J-07) Plug offline event after 3 failed rounds; timer mismatch event from a daily read-back. AC: fixture with a changed timer raises 1 mismatch event.
- FR-12 [S] (J-07) Grid events: all plugs fail in the same round (power cut); voltage outside the configured band. Shown only, never acted on. AC: fixture power cut raises 1 grid event, not 10 offline events.
- FR-13 [M] (J-04, J-05) Cooldown and dedupe: one event per plug and kind per 24 h. AC: a 6-hour anomaly raises 1 event, not 24.

Agents and proposals
- FR-14 [M] (J-04) Proposal contract: device, timer changes, reason, savings (kWh, LKR, kgCO2e per week), evidence, source (agent or chat), expiry. AC: schema validated before the inbox.
- FR-15 [M] (J-04) Responder turns one event into 0 or 1 proposal. AC: every number in its text appears in the computed facts; no proposal for a device with access none.
- FR-16 [M] (J-05) Planner runs weekly and on goal drift: goal status, month projection, solver re-plan, weekly report. AC: on a fixture month over goal, the plan meets the goal or reports the shortfall in kWh.
- FR-17 [S] (J-06) Assistant answers questions in chat with read tools; changes become proposals or go to the Planner. AC: has no write tools; a request touching an access-none device is refused with the reason.
- FR-18 [M] (J-04) Inbox: approve, edit, or reject with a reason, in the web UI and through phone notification buttons. AC: an expired proposal can't be approved; an edited proposal is checked again before it is saved.
- FR-19 [M] (J-08) Shadow mode toggle. AC: with shadow mode on, approved proposals are recorded and the Actuator makes 0 writes.

Acting
- FR-20 [M] (J-04) Actuator: on approval, re-check rules and access, write timers only in allowed slots, read back, write an audit row. AC: a read-back mismatch sets the proposal to failed and notifies; no write outside allowed slots.
- FR-21 [M] (J-10) Audit every proposal, decision, write and read-back, with time, actor and reason. AC: history shows the full chain for any timer on any plug.

Goal and carbon
- FR-22 [M] (J-05) Monthly kgCO2e goal set by the owner; status (on track, at risk, off track) computed in code; a goal change only by proposal. AC: fixture months give on track, at risk and off track at the configured margins; the goal can't be edited outside a proposal.
- FR-23 [M] (J-04, J-05) kWh, LKR and kgCO2e computed in code; each factor stored with its source and date; tariff from config. AC: unit tests against hand-worked values.
- FR-24 [S] (J-01) Baseline for a device with no data: rated W × duty cycle, replaced by the profile after 7 days. AC: a new device has a baseline on day 1; on day 8 the profile is used.

Learning, history, cost
- FR-25 [S] (J-09) Store rejection reasons as preferences the agents read; don't re-propose the same change for that device within 14 days unless asked. AC: after a rejection, the same change for that device is not proposed in a 14-day replay.
- FR-26 [S] (J-09) Memory page: profiles, preferences, thresholds; view, edit, delete each. A "false alarm" mark raises that plug's threshold. AC: an edit or delete takes effect on the next run; a false-alarm mark changes the threshold and is audited.
- FR-27 [S] (J-10) Trace every LLM call: prompt, output, tokens, USD, latency, linked to its event or chat. AC: every LLM call in a test run has a trace row; monthly USD on the history page equals the sum of rows.
- FR-28 [M] (J-10) Budget caps per run and per month. AC: at the cap, agents stop and events queue for the next weekly Planner run.

Later (not v1)
- FR-29 (J-11) Emission factor revision: document uploaded in chat, vector search, extracted factor as a proposal with page and source.

## 5. Non-functional requirements
Reference machine: HP ProBook G5, 16 GB, i5, CPU only. Reference site: 10 plugs.

Cost
- NFR-01 LLM spend ≤ USD 3 a month at the reference site, enforced by the cap (FR-28).
- NFR-02 A Watcher pass makes 0 LLM calls.

Performance
- NFR-03 A Watcher pass takes ≤ 10 s.
- NFR-04 A proposal reaches the inbox ≤ 20 min after its condition starts.
- NFR-05 Responder ≤ 30 s per event; Assistant first reply ≤ 10 s; Planner ≤ 5 min.
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
- NFR-15 100 simulated plugs for a month: LLM spend under the cap, Watcher pass ≤ 10 s, no lost rounds.

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
- SM-02 Shadow mode, 2 weeks: at least 60% of proposals approved without edits.
- SM-03 Event precision at least 80%: of the events raised, the share I label as real.
- SM-04 kWh a month saved by approved changes, against the hour-of-week baseline, reported with its EnPI. No target until shadow mode ends; the number is reported either way.

Portfolio
- SM-05 Demo with approval running, before interviews: a video of 3 min or less and the README results table filled.
- SM-06 One write-up post linked from the README and the CV.

Process
- SM-07 At release, every FR links to at least one test or eval, and every gate is passed or overridden with a reason.
- SM-08 A retrospective after v1: what to change in the process.

## 7. Constraints and assumptions
Constraints (fixed)
- C-01 Solo. Claude drafts code; I write and edit parts, own the requirements and design calls, and review.
- C-02 One hub: ProBook, 16 GB, CPU only, OS on a pen drive, data on an SD card. The logger already runs on it.
- C-03 No paid infrastructure; the only spend is model API calls (NFR-01).
- C-04 Devices are Tasmota plugs over HTTP: 16 timers each, weekly repeat, local time.
- C-05 Public repo: no AgentC source, thesis results or private addresses.
- C-06 Python via uv; Next.js static export for the UI; Claude models, tiered by cost.
- C-07 Shares time with job applications: musts first, shoulds only if on track.

Assumptions (verify; revisit if wrong)
- A-01 Writing a timer doesn't restart the plug or cut its power. Verify on one low-risk plug before FR-20.
- A-02 Plugs keep correct time (NTP) in Asia/Colombo, so timers fire when planned. Verify `Time` and `Timezone` on each.
- A-03 One grid factor for Sri Lanka is a sound v1 method (Scope 2, location-based).
- A-04 The hour-of-week profile is good enough for 80% event precision (SM-03).
- A-05 Events stay at a few a week, which keeps cost under the cap (NFR-01).
- A-06 ntfy action buttons can call the API over Tailscale from the phone.
- A-07 CP-SAT solves a week for 10 plugs within NFR-05 on the hub.

## Open questions
- Which grid emission factor for Sri Lanka, from which publisher and year?
- My tariff: block or time-of-use? Shifting load only saves money under time-of-use.
- The monthly kgCO2e goal: what value to start with?
- ntfy: the public ntfy.sh server or self-hosted on the hub? Who can read the messages?
- How do phone approval buttons authenticate to the API?
- Does Electricity Maps cover Sri Lanka with hourly intensity?
