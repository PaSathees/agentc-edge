# AgentC audit

Input to Specify. Source: `~/Desktop/PROJECTS/agentc` (backend about 9,000 lines of Python, Next.js frontend). 2026-11-08.

Verdicts: **keep** (port as is, simplified) · **replace** (same function, new owner) · **drop**.

## Cloud calls in the old system
| Node | Active model | Tools it could call |
|---|---|---|
| Manager (between every worker) | Claude Sonnet 4.6 | none |
| Optimisation expert | GPT-5.1 | calculator, python_repl |
| Scheduling expert | Claude Opus 4.5 | none |
| Validation expert | Claude Opus 4.5 | none |
| Comfort expert | GPT-4.1 | none |
| Baseline estimation | GPT-4.1 | Serper web search |
| Goal assessment | GPT-5.1 | none |
| Emission factor resolution | GPT-5.1 | DB, ChromaDB, Serper |
| Usage pattern learning | Claude Opus 4.5 | Serper web search |
| Vector store | OpenAI embeddings | n/a |

9 LLM nodes from 2 providers, plus a paid search API. 3 of them are Opus-class, the manager runs after every worker, and the planning loop repeats. Ran daily through `/scheduled-run`.

## Workers and graph
| Old | What it did | Verdict | New |
|---|---|---|---|
| Manager agent | LLM picks the next worker from a todo list | replace | Code routing; agents wake on events, weekly or chat |
| `data_collection` | Org, devices, last run | keep | Device registry read from SQLite |
| `iot_data_processing` | Usage sessions from InfluxDB | replace | Logger SQLite reader plus quality checks |
| `emission_factor_resolution` | LLM, 3 tiers on every run | replace | Cached factor per energy source, with its source; retrieval only on a miss, approved |
| `baseline_estimation` | LLM guesses profiles from specs | replace | Code template (rated W × duty cycle); LLM fills specs once, at setup |
| `usage_pattern_analytics` | Code: sessions, temporal stats, pattern type | keep | Hour-of-week profile per plug (the forecast v0) |
| `usage_pattern_learning` | LLM rewrites device profiles | replace | Code updates profiles; rejection reasons go to a preferences table |
| `goal_assessment` | LLM: on-track/at-risk/off-track, may adjust the goal | replace | Code computes status; a goal change becomes a Planner proposal |
| `simulation_worker` | Projects the month from the schedules | keep | Code, a Planner tool, weekly |
| `commit_worker` | Writes results and plans to the DB | replace | Actuator writes timers after approval; audit row |
| Optimisation expert | LLM plan to meet the goal | replace | CP-SAT solver, a Planner tool |
| Comfort expert | LLM checks comfort | replace | Hard constraints from device constraints, approved once |
| Validation expert | LLM checks hierarchy and constraints | replace | Code policy check before a proposal and in the Actuator |
| Scheduling expert | LLM writes on/off date ranges | replace | Solver output mapped to Tasmota weekday timers plus an expiry |
| `initialize`, `finalize`, `node_tracker`, `state_differ` | Custom run and node tracing into Postgres | replace | One trace table (or OpenTelemetry); prompt, output, tokens, cost per call |
| `summarizers` | Shrinks long state for prompts | drop | Small prompts; nothing to summarise |

## Tools
| Old | Verdict | Note |
|---|---|---|
| `calculate_emissions`, `calculate_emissions_for_sessions` | keep | Code, unit tested |
| `simulate_scheduled_actions` | keep | Becomes `simulate(schedule)` |
| `calculator`, `code_interpreter`, `python_repl` | drop | The LLM no longer computes; an LLM running code is a security risk |
| `query_device_consumption`, `fetch_and_aggregate_usage_sessions` | keep | Rewritten for SQLite |
| `query_device_status` | keep | Tasmota `Status` |
| `query_environmental_sensors` | replace | Open-Meteo weather now; a Tasmota temperature sensor later. Comfort comes from user rules |
| `database_tools` (20 tools, 1,033 lines) | replace | Small repo module; no LLM gets DB write tools |
| `search_web`, `search_emission_factor_web` (Serper) | drop | Paid key, and the results can't be checked |
| `vector_store_tools` (ChromaDB, OpenAI embeddings) | later | Only for user-started factor revisions: a document uploaded in chat → light vector search (sqlite-vec, local embeddings) → factor proposal → approve → stored with its source. Never called by agents on their own |

## Data and runtime
| Old | Verdict | New |
|---|---|---|
| Postgres, 19 tables (Prisma + SQLAlchemy) | replace | SQLite, fewer tables |
| InfluxDB | drop | Logger SQLite |
| `Devices`, `DeviceConstraints` (usage, priority, time window), `EnergySources` (renewable or grid, factor), `Rooms` | keep | The registry. Rooms become an optional field |
| `DevicePlans`, `ScheduledActions` | replace | Proposals plus timers on the plugs |
| `EmissionData`, `SimulationData` | keep | Results tables |
| Room and device snapshot tables | drop | The audit row stores a JSON snapshot |
| `NoderedState` (Node-RED applied the plans) | drop | Tasmota timers run on the device |
| `/run-agent`, `/scheduled-run` (daily) | replace | Events, weekly Planner, chat |
| Railway hosting | drop | ProBook, Tailscale |

## Frontend
| Old page | Verdict | New |
|---|---|---|
| Login | drop | Tailscale only |
| Setup, Rooms | keep | One setup page: devices, constraints, energy sources, goal |
| Dashboard | keep | Plugs, goal, month projection |
| History | keep | Runs, proposals, audit, traces |
| About | drop | |
| none | new | Approval inbox, chat, shadow mode toggle |

## Functions the rebuild must keep (feed into FRs)
- Monthly kgCO2e goal, with status and a goal change by proposal
- Device constraints: usage, priority, allowed time window (now user rules, hard or soft)
- Renewable vs grid energy sources, each with a factor and its source
- Baseline for devices with no data yet
- Plan to meet the goal, applied as schedules
- Month projection
- Run history and trace
