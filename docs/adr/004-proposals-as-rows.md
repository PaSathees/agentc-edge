# ADR-004: Proposals are rows, not paused agent runs

Status: accepted
Date: 2026-11-12
Serves: FR-14, FR-18, FR-20, NFR-10

## Context

Human approval can be built by pausing the agent graph at an interrupt and resuming it when the owner answers. Approvals can take hours, proposals expire, the owner may edit them, and the hub may restart in between.

## Options
1. Pause the agent run at an interrupt; resume on the owner's answer.
2. End every agent run by saving a proposal row. Approval is a status change on the row; the Actuator, which is plain code, acts on it.

## Decision

Option 2, for all three agents. The LangGraph checkpointer is kept only for chat threads.

## Consequences

- No agent run is ever waiting, so a restart loses nothing (NFR-10) and nothing holds memory.
- One inbox and one format for every source: Responder, Planner and chat (FR-14).
- Edits make a new version that the checks run again; an approval binds to one version.
- Expiry is a timestamp, not a timeout inside a graph.
- An agent cannot react to the owner's answer in the same run. Reject reasons reach agents through the memory instead (FR-25).
