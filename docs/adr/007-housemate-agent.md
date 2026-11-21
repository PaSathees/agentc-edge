# ADR-007: Agents with a shared house model and read tools

Status: accepted (changed in the owner's review, 2026-11-21: three agents on a shared base, not one agent)
Date: 2026-11-21
Serves: FR-15, FR-16, FR-17, FR-30, FR-31, FR-33

## Context

In version 1, code built the fix options (off and on times, with savings) and the model only picked A, B or none and wrote a reason. In 14 real days it made 0 proposals, and on 3 public homes it found 0 fixes. Most unusual use was real use at an odd hour, which needs judgment, not a timer. The model could not judge, because all it had was the device's name and its usual week.

## Options
1. Keep code-built options and add more rules to tell waste from real use.
2. One agent with a house model and read tools, started by a signal, a chat message or the weekly job.
3. Separate agents (housemate, chat, Planner) on one shared base: house model, tool library, checks, `llm()`.
4. A free agent with write tools.

## Decision

Option 3. Each agent has its own prompt, its own subset of tools, its own model and its own evals. They share the house model, so each has the whole view, and the tool library, checks and `llm()`, so there is one copy of each. Every output goes through the checks. No agent writes to a plug.

## Consequences

- The judging moves to the AI, where it is strong. Code shrinks: the gap and options logic goes.
- Each agent can change and be tested alone. The Planner can later get forecasting tools and a bigger model without touching the housemate's prompt (the reason option 2 lost in review).
- Answers vary between runs, so evals matter more. The owner's answers become labelled cases, so the eval set grows with use.
- More tokens per signal than one pick: up to 6 tool calls, about USD 0.002 to 0.003 per signal (estimate from version 1's measured calls). Signals are few, so the cost stays well under the USD 3 cap.
- Option 4 is out: no agent writes. Option 1 is what version 1 tried.
