# ADR-009: Our own small agent loop on llm(), not LangGraph

Status: accepted
Date: 2026-11-21
Serves: FR-15, FR-28, NFR-10

## Context

An agent with tools runs a loop: the model asks for a tool, code runs it, the result goes back, until the model answers or a limit is hit. Version 1 planned LangGraph for the Assistant and Planner. ADR-004 already keeps every run short: a run ends in a row (question or proposal), and nothing waits inside a graph for the owner.

## Options
1. LangGraph: graphs, checkpoints, a recursion limit.
2. Our own loop on top of `llm()`: about 30 lines, the 6-call limit and budget in code we can read.

## Decision

Option 2. The loop lives next to `llm()`, so every step is traced and capped in one place.

## Consequences

- One less framework to learn and to keep up to date. The whole loop fits on one screen.
- No pause and resume inside a run, which ADR-004 does not need.
- If chat later needs long threads that survive restarts, LangGraph's checkpointer is the first thing to try, in its own ADR.
