# ADR-003: A solver builds the schedule; the model explains it

Status: accepted
Date: 2026-11-12
Serves: FR-05, FR-16, FR-23, NFR-11

## Context

AgentC planned with LLM nodes that proposed, calculated and validated schedules in a loop. Models did arithmetic, the loop repeated, and nothing guaranteed the owner's rules were kept.

## Options
1. LLM planning loop, as before, with better prompts.
2. A constraint solver (OR-Tools CP-SAT) builds the week: hard rules as constraints, soft rules as costs. The model may adjust soft-rule weights, picks among solved plans, and explains the result.
3. Fixed heuristics in code only, no model.

## Decision

Option 2. The Planner calls the solver as a tool. A plan is accepted only if it matches a solver output exactly.

## Consequences

- Hard rules always hold, and the numbers are right by construction (NFR-11).
- One solve takes seconds for 10 plugs (A-07), instead of several expensive model turns.
- The model's job shrinks to choosing and explaining, so a mid-tier model is enough.
- Rules must be expressible as constraints. Rules that cannot be expressed are rejected at entry with a reason, not handed to the model.
- Option 3 would lose the explanation and the trade-off between soft rules that the owner sees.
