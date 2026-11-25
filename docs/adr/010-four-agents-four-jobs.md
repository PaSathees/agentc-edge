# ADR-010: Four agents, four jobs

Status: accepted
Date: 2026-11-25
Serves: FR-17, FR-30, FR-36, NFR-12
Amends: ADR-007

## Context

ADR-007 set up agents on a shared house model. In practice the housemate did two jobs. It caught waste happening now, and it also proposed long-run fixes, such as moving a timer. On 25 Nov it wanted to move the PeoTV box's 06:00 timer to 07:00. That is a planning question, so it fell out as a long note with no way to act on it. Each long-run idea would otherwise become a question, and the owner would get too many interruptions.

## Options
1. Keep one housemate that both asks and proposes, and limit how often it proposes.
2. Split by job and by time scale: each agent has one job, one rhythm, and one way to reach the owner.

## Decision

Option 2:

| Agent | When | Sees | Gives | May write |
|---|---|---|---|---|
| Housemate | a signal, now | one device | ignore, a one-sentence note, or a question when a tap now saves energy | switch off now, after the owner's tap |
| Planner (Phase 5b) | once a week | the whole house, the week | one plan of few items, each before and after with its saving | timer changes, the owner's own slots too, only after Do it |
| Assistant (Phase 5d) | when the owner asks | anything, read only | answers, explanations, drafts; hosts the weekly review (5c) | nothing by itself |
| Setup agent (since 3d3) | when the owner sets up or edits a device | that device | a draft card | nothing: only the owner's Save |

The housemate no longer proposes and never offers a timer. Anything about the long run becomes a one-sentence note, and the Planner reads every note each week. Code checks this: the housemate's actions are ignore, note and ask, and its choices are switch off now, save a habit, remind later, or nothing.

## Consequences

- At most one weekly message for plans, plus questions only for waste happening now.
- The housemate is simpler to test: its evals score one job.
- Timer writes from a tap on a question are no longer offered. The Actuator keeps its timer code for the Planner.
- In 5d, decide whether the setup agent stays separate or becomes one mode of the Assistant.
