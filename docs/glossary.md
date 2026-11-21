# Glossary

Plain words for the codes and terms in these docs.

## Codes

| Code | Means | Where |
|---|---|---|
| J-01 | A job the owner wants done: "When ..., I want ..., so I can ..." | [requirements](requirements.md) 2 |
| FR-01 | Something the system must do (functional requirement) | [requirements](requirements.md) 4 |
| NFR-01 | A limit on how well it does it: speed, cost, safety (non-functional requirement) | [requirements](requirements.md) 5 |
| SM-01 | How we judge success (success measure) | [requirements](requirements.md) 6 |
| C-01 | A fixed limit we can't change (constraint) | [requirements](requirements.md) 7 |
| A-01 | Something we believe but must check (assumption) | [requirements](requirements.md) 7 |
| AC | Acceptance criteria: the test that proves a requirement is met | next to each FR |
| [M] [S] | Priority: M = must have, S = should have | next to each FR |
| EV-01 | A test case for the AI agents (evaluation case) | [evaluation](evaluation.md) |
| ADR-001 | A design decision and why it was made (architecture decision record) | [adr/](adr/) |

## Parts of the system

| Term | Means |
|---|---|
| Hub | The old laptop at the site that runs everything |
| Stage | One step of the method: specify, design, build, evaluate, operate ([process](process.md)) |
| Phase | One step of the build roadmap, 1 to 6 (README). Each ends in one merged pull request |
| Logger | A small program that reads every plug once a minute into `energy.db` |
| Watcher | Version 1: code that checks the readings every 15 minutes and records events. It never calls the AI. Becomes Signals in version 2 |
| Event | Something worth a look: unusual use, a plug offline, lost contact with every plug, the logger stopped, a timer changed on a plug |
| Agent | An AI model given a job and a few tools. Version 2: the housemate (signals), chat and the Planner, three agents on a shared base. Version 1 called them Responder, Assistant and Planner |
| Proposal | A suggested timer change, with its reason and savings, waiting for approval |
| Actuator | The only code that writes to plugs, and only after approval |
| Read-back | After a write, the Actuator reads the plug again to check the change took |
| Shadow mode | The agent asks and suggests, but nothing is written to any plug |
| House model | What the system knows about the home: a card per device, habits, timers with their reason, the plan |
| Device card | One device: what is plugged in, its kind, room, must it run, notes. Filled by the setup chat |
| Habit | Something normal for you, like "Desk 2 runs until 02:00 on Thursdays". Comes from your answers |
| Signal | Something code found worth a look: left on, unusual use, automation drift, a new device, a plug offline. v2 name for an event |
| Housemate agent | The agent that looks into a signal with tools, then ignores, notes, asks you, or proposes. One of three agents on a shared base |
| Question | What the agent asks you, with 2 or 3 choices. Each choice maps to an action code knows |
| Automation drift | A timer keeps a plug on, but the device only draws standby: on for nothing |
| Change point | The moment a plug's power pattern shifts for good, usually a new device on it |
| Quiet hours | Hours with no questions (default 23:30 to 07:00) |
| Solver | A maths tool that finds the best timetable within the rules. The Planner uses it |
| Tier | A size of AI model: small for quick jobs, mid for planning |
| Token | A piece of text the AI reads or writes. AI cost is counted in tokens |

## Data and checks

| Term | Means |
|---|---|
| Profile | A plug's usual use for each of the 168 hours of the week |
| Median | The middle value. One odd reading doesn't move it, unlike the average |
| On share | The share of minutes a plug draws more than 2 W in that hour |
| Run | A stretch of unusual use: 10 W more for 30 minutes, or 3 W more for 3 hours |
| Round | One pass of the logger over all plugs, once a minute |
| Fixture | A small, made-up set of data used by a test |
| Grounded | Every number in an agent's text matches a number code gave it |
| Judge | A second AI model that scores how clear an agent's wording is |
| Precision | Of the events raised, the share that were real |

## Energy and carbon

| Term | Means |
|---|---|
| kWh | A unit of energy. 1 kWh = 1,000 W for 1 hour. The bill counts these as "units" |
| kgCO2e | Kilograms of greenhouse gas, counted as if it were all CO2 |
| Grid factor | kg of CO2 per kWh from the grid. Sri Lanka 2023: 0.4242 (SLSEA) |
| Scope 2 | The carbon from electricity you buy. "Location-based" uses the grid's average factor |
| Baseline | Use before any change, to compare savings against |
| EnPI | Energy performance indicator: one number that tracks energy use, such as kWh a day |
| Block tariff | Price per unit that steps up as the month's total goes up |
| Time-of-use | Price per unit that depends on the time of day |
