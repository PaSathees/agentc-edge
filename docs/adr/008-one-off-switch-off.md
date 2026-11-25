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

## Amendment, 2026-11-25

The owner may also switch a device on or off from the Devices page, while logged in. A tap there is the owner's tap, like a tap on a question. It goes through the same Actuator and checks: access *write*, never off for a must-run device, shadow mode and the login, at most 4 writes a plug a day, read back, audit row. Why: the first real write showed that waiting for a question to test or fix one device is slow, and the owner already has this switch on the plug's own page. The page shows each write device's state, asked live, and no switch when the plug does not answer.
