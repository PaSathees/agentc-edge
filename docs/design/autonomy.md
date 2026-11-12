# Autonomy map

[Design overview](../design.md)


Who does each step. The rule: **code** computes every number, checks every rule and makes every write. The **AI model** does four kinds of work only:

- **explain:** turn computed facts into a short reason
- **choose:** pick one of the options code has already worked out
- **translate:** turn plain language into a structured rule or request
- **converse:** chat

The **owner** approves every change.

| D | Step | Code | AI model | Owner | Requirement |
|---|---|---|---|---|---|
| D-01 | Read plugs | reads power, voltage, current every minute and stores them | | | NFR-07 idle load |
| D-02 | Import timers | reads each plug's 16 timers as the current schedule | | | FR-01 timer import |
| D-03 | Usage profiles | daily: typical power for each hour of the week, per plug | | | FR-02 profiles |
| D-04 | First proposals | compares the timers with real use; computes savings where they differ | explains each gap | approves | FR-03 first proposals |
| D-05 | Discover plugs | scans the network; matches plugs by MAC | asks setup questions in chat | answers | FR-06 discovery |
| D-06 | Device change | notices a plug's power pattern no longer fits its profile | explains what changed | confirms the new device | FR-07 device change |
| D-07 | Device setup | stores access level and timer slots | | sets them | FR-04 device setup |
| D-08 | Rules | checks each rule is valid; enforces it | turns "keep the TVs on until 1 am" into a rule | approves the rule | FR-05 rules |
| D-09 | Weather | fetches the forecast daily | | | FR-08 weather |
| D-10 | Watch | every 15 min: unusual use, goal drift, offline plug, timer mismatch, grid event; at most one event per plug and kind a day | | | FR-09 to FR-13 events |
| D-11 | Respond to an event | works out the options and their savings | picks one and explains it (Responder) | approves | FR-15 Responder |
| D-12 | Check proposals | format, numbers match the facts, rules, access | | | FR-14 contract |
| D-13 | Goal status | month so far, projection, on track or not | | sets the monthly goal | FR-22 goal |
| D-14 | Re-plan the week | solver builds a schedule that keeps every hard rule | explains it; writes the weekly report (Planner) | approves | FR-16 Planner |
| D-15 | Chat | answers with read-only tools; passes schedule changes to the Planner | chats; turns requests into structured ones (Assistant) | asks | FR-17 Assistant |
| D-16 | Inbox | expires old proposals; checks an edited one again | | approves, edits or rejects | FR-18 inbox |
| D-17 | Shadow mode | when on, no write reaches a plug | | switches it | FR-19 shadow |
| D-18 | Write timers | checks again, writes, reads the timer back | | | FR-20 Actuator |
| D-19 | Audit | records every proposal, decision and write | | | FR-21 audit |
| D-20 | Carbon and cost | kWh, LKR, kgCO2e; each factor with its source | | sets factor and tariff | FR-23 maths |
| D-21 | Baseline for a new device | rated watts × share of time on | | gives rated watts | FR-24 baseline |
| D-22 | Learn from rejections | keeps the reason; no repeat of the same change for 14 days | turns the reason into a soft rule | edits it on the device page | FR-25 preferences, FR-26 memory |
| D-23 | Alert thresholds | raises a plug's threshold when an alert is marked "false alarm" | | marks false alarms | FR-26 memory |
| D-24 | Trace and budget | logs every model call and its cost; stops at the cap | | sets the caps | FR-27 trace, FR-28 budget |
| D-25 | Factor revision (later) | searches the uploaded document; stores the factor | reads the factor from the document | approves | FR-29 factor revision |

The AI model takes part in 9 of 25 steps (D-04, 05, 06, 08, 11, 14, 15, 22, 25; 8 in v1). In none of them does it write, compute a number or decide alone.

Workflow or agent: every step runs in a fixed order set by code, except the three agents, which choose their own tool calls within a limit: Responder up to 3, Planner up to 8, Assistant up to 5 per chat turn.
