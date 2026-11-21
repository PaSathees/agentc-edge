# ADR-008: One-off "switch off now" after the owner's tap

Status: accepted
Date: 2026-11-21
Serves: FR-20, FR-32, NFR-12
Amends: ADR-002

## Context

ADR-002 says schedules live on the plugs as timers, with no direct on or off commands. A timer only helps future nights. When a device is left on right now, the useful answer is "switch it off now".

## Options
1. Timers only. The waste of tonight is lost.
2. Allow one `Power Off` command, only after the owner taps "Switch off" on a question for that device, logged and read back.
3. Let the agent switch devices off on its own.

## Decision

Option 2. Schedules stay as timers (ADR-002). A one-off off is the only direct command, and only after a tap.

## Consequences

- Waste can stop while it happens, not just from the next night.
- It is easy to undo: switch on at the plug, or from the inbox.
- The same write-time checks apply: access *write after approval*, a valid single-use token, at most 4 writes per plug a day, read back, audit row. Shadow mode records it and sends nothing.
- Never sent to a must-run device or a device with access *none*.
- Option 3 is "earned autonomy" and stays later, per device, after many approvals in a row.
