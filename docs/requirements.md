# Requirements

## 1. Problem and context

Small sites (a home now, a plant room later) pay for energy they don't need: devices left on, schedules that no longer match use, no one watching the data. Scope 2 emissions follow the same kWh.

AgentC (MSc prototype) showed an agent can plan device schedules to a monthly kgCO2e goal. As a system it fell short:
- cost: 9 LLM nodes, daily, about USD 0.60 a run, even when nothing changed
- control: plans in Postgres, applied by Node-RED; no human approved anything
- trust: LLMs did the maths, routing and checks; nothing was evaluated
- reach: cloud only (Railway, Postgres, InfluxDB)

Want: the same functions, running on a box inside the site, where code watches all the time, agents wake only when something happens, and a person approves every change before it reaches a device.

- Site v1: my home, 10 Tasmota plugs, logger running since 2026-11-01
- User v1: one owner who sets the rules and approves changes
- Mode: propose and approve; shadow mode first
- Evidence: `audit.md` (old system, keep, replace, drop)

## 2. Users and job stories
Format: `J-xx When [situation], I want [action], so I can [outcome].`

## 3. Scope
In (v1):

Later:

Out:

## 4. Functional requirements
Format: `FR-xx [M/S] (J-xx) what it does. AC: acceptance criteria.`

## 5. Non-functional requirements
Reference machine: HP ProBook G5, 16 GB, i5, CPU only.

## 6. Success measures

## 7. Constraints and assumptions
Constraints (fixed)

Assumptions (verify; revisit if wrong)

## Open questions
