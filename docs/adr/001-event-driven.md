# ADR-001: Agents wake on events, not on a daily run

Status: accepted
Date: 2026-11-12
Serves: FR-09 to FR-13, NFR-01, NFR-02, NFR-04

## Context

AgentC ran its whole agent graph once a day, about USD 0.60 a run, whether or not anything had changed. On most days nothing had. Its cost grew with the number of runs, not with the number of problems.

## Options
1. Daily run, as before, with cheaper models.
2. Plain code watches the data every 15 minutes and raises events; agents run only on an event, on the weekly plan, or in chat.
3. Stream processing with a rules engine.

## Decision

Option 2. The Watcher is plain code with no model calls. It raises an event only for unusual use, goal drift, an offline plug, a timer mismatch or a grid event, at most one per plug and kind a day. Agents start from events.

## Consequences

- Cost follows problems: a quiet day costs nothing, and the cap holds at USD 3 a month (NFR-01).
- Latency improves: a proposal arrives within 20 minutes of the cause instead of the next daily run (NFR-04).
- Event quality now matters most. A noisy Watcher wastes calls and the owner's attention, so event precision is measured (SM-03) and false alarms raise thresholds.
- Option 3 is more machinery than 10 plugs need; the 15-minute pass takes seconds.
