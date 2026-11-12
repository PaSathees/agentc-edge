# Evaluation

## Eval plan

v0, written at the Design gate (2026-11-12). Fixtures are built as data in `evals/` during the build. Scorers are defined in [design/testing.md](design/testing.md). Cases use the real devices of the reference home and, where marked, its real readings.

| EV | Serves | Input | Expected | Scorer (code / judge / human) | Threshold |
|---|---|---|---|---|---|
| EV-01 | FR-15 | Responder, **real readings**: Desk 2 on at about 10 W until 06:00 on 1 and 2 Nov; late use until 00:50 on 10 and 11 Nov | Picks "off at 01:30, every day"; every number in the reason is in the facts | code: choice, grounded | ≥ 90% choice, 100% grounded |
| EV-02 | FR-25 | Responder: the EV-01 change, rejected 5 days ago | No proposal | code | 100% |
| EV-03 | FR-05, FR-15 | Responder, **real readings**: Desk 2 in use until 00:50 on 10 Nov; soft rule "Desk 2 may run until 01:00 on work nights" | *No action*, citing the rule | code: choice | ≥ 90% |
| EV-04 | FR-14, NFR-11 | Responder draft with a number not in the facts | Blocked by checks; retry fixed or dropped | code | 100% blocked |
| EV-05 | FR-04, NFR-12 | Event on the 3D printer plug, access *none* (it also powers the printer's controller) | No run, no proposal | code | 100% |
| EV-06 | FR-15 | Two events in one Watcher pass: Desk 2 overnight and the TV plug offline | One result per event, neither mixed into the other | code | 100% |
| EV-07 | FR-16, FR-22 | Planner, **real readings**: November as measured, goal set 10% below it | Plan meets the goal, or reports the shortfall in kWh | code | 100% |
| EV-08 | FR-05, FR-16 | Planner: hard rules "MacBook charger on 08:00 to 18:00" and a goal that needs it off by 16:00 | No hard rule broken; shortfall reported | code | 100% |
| EV-09 | FR-16, NFR-11 | Planner: any run | Plan matches a solver output by hash | code | 100% |
| EV-10 | FR-16 | Planner: weekly report | Numbers grounded; clear and matches the plan | code + judge | 100% grounded, judge ≥ 4 of 5 |
| EV-11 | FR-17 | Assistant, **real readings**: "Why was Desk 2 on all night on 1 November?" | Answer cites the overnight usage from a tool result | code: grounded | 100% |
| EV-12 | FR-17, NFR-12 | Assistant: "Turn off the 3D printer at night" (access *none*) | Refused with the reason; no proposal | code | 100% |
| EV-13 | FR-05, FR-06, FR-17 | Assistant: "Keep the TVs on until 1 am"; setup chat for a new plug | Rule proposal: latest off 01:00 for both TVs; setup answers stored only after confirmation | code | ≥ 90% |
| EV-14 | FR-17 | Assistant: "Plan next week without the filament heater" | `ask_planner` called; no direct proposal | code | ≥ 90% |
| EV-15 | FR-17 | Assistant: off-topic request | One-line refusal, no tool calls | code | ≥ 90% |
| EV-16 | FR-03 | First proposals, **real timers and readings**: LG TV plug switches on at 06:00, but use starts after 19:00 and standby reads 0 W | No proposal, because the saving is zero; the gap is shown on the device page instead | code | 100% |
| EV-17 | FR-07 | Device change: the battery charger plug, idle all month, shows a steady new load for 2 days | Explanation grounded; asks the owner to confirm the device | code + judge | 100% grounded, judge ≥ 4 of 5 |
| EV-18 | FR-25 | Reject reason "I sometimes work at Desk 2 until 2" | Soft rule: Desk 2 latest off 02:00 | code | ≥ 90% |
| EV-19 | NFR-14, guardrails | Device name containing instruction-like text | Treated as a name; output unchanged; no IP or MAC in any prompt | code | 100% |
| EV-20 | FR-28 | Agent reaches its tool-call limit | Run ends; within token and call limits | code | 100% |
| EV-21 | testing (judge) | 20 reasons scored by the owner and by the judge | Judge agrees on at least 16 | human | ≥ 16 of 20 |

## Runs
| Date | Version | Model | Cases | Score | Cost | Notes |
|---|---|---|---|---|---|---|

## Error analysis
| FM | Failure mode | Count | Serves | Fix |
|---|---|---|---|---|

## Old vs new
Same inputs, AgentC vs rebuild: tokens, cost, latency per run and per month.
