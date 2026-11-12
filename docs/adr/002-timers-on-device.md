# ADR-002: Schedules run as timers on the plug

Status: accepted
Date: 2026-11-12
Serves: FR-01, FR-20, NFR-06, NFR-09, NFR-12

## Context

AgentC stored plans in Postgres and Node-RED switched the plugs. If the server, the network or Node-RED stopped, the schedule stopped. Every switch was a live command from software.

## Options
1. A control loop on the hub sends on and off commands at the planned times.
2. Write the schedule into the plug's own timers (Tasmota `Timer1` to `Timer16`), after approval.

## Decision

Option 2. The Actuator writes timers only in the slots the owner allows, then reads them back. No direct on or off commands.

## Consequences

- The plug keeps its schedule when the hub, the app or the internet is down (NFR-09).
- One approved change is one small write, easy to audit and to read back (NFR-06).
- Limits: 16 timers per plug, weekly repeat, minute resolution, no conditions. Enough for on and off schedules; not for "off when the room is empty".
- The plug's clock must be right (A-02), and a timer write must not restart the plug (A-01). Both are checked before the first write.
- Timers the owner set by hand are imported as the starting schedule and never changed.
