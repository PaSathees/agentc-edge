# Evaluation

## Eval plan

v0, written at the Design gate (2026-11-12). Fixtures are built as data in `evals/` during the build. Scorers are defined in [design/testing.md](design/testing.md). Cases use the real devices of the reference home and, where marked, its real readings.

| EV | Serves | Input | Expected | Scorer (code / judge / human) | Threshold |
|---|---|---|---|---|---|
| EV-01 | FR-15 | Responder, **real readings**: Desk 2 on at about 5 W until 06:00 on 1 and 2 Nov; late use until 00:50 on 10 and 11 Nov | Picks "off at 01:30, on at 07:00", every day (A) or weekdays (B): both waste nights were weekdays, so both save the same; every number in the reason is in the facts | code: choice, grounded | ≥ 90% choice, 100% grounded |
| EV-02 | FR-25 | Responder: the EV-01 change, rejected 5 days ago | No proposal | code | 100% |
| EV-03 | FR-05, FR-15 | Responder, **real readings**: (a) Desk 2 in use until 00:42 on 11 Nov; (b) the EV-01 case with the soft rule "Desk 2 may run until 02:00 on work nights" | (a) No fix, so the model is not asked; (b) *No action*, citing the rule | code: choice | (a) 100%; (b) ≥ 90% |
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

Where EV-01 to EV-06 run (B3, 2026-11-17):

- EV-01, EV-03 (b): `agentc eval --live` asks the real model 10 times each and records the answers in `evals/recorded.json`; `tests/test_evals.py` replays them on every commit. Live again when a prompt, model or tool changes.
- EV-03 (a), EV-06: `tests/test_evals.py`, on the same real readings.
- EV-02: `tests/test_responder.py` and `tests/test_jobs.py`. EV-04: `tests/test_responder.py`. EV-05: `tests/test_responder.py` and `tests/test_jobs.py`.

## Runs
| Date | Version | Model | Cases | Score | Cost | Notes |
|---|---|---|---|---|---|---|
| 2026-11-17 | responder-v1 | claude-haiku-5-5 | EV-01, EV-03 (b), 10 tries each | EV-01 10/10 right, 10/10 checks, picked A 10. EV-03 9/10 right, 10/10 checks, picked none 9, A 1. Both PASS | USD 0.0053 (EV-01 0.0013, EV-03 0.0040) | No retries. EV-03 calls cost about 3 times more: likely more thinking when a rule must be weighed. Reasons are 2 long sentences, not short ones: for the judge (EV-21) |

## Error analysis
| FM | Failure mode | Count | Serves | Fix |
|---|---|---|---|---|
| FM-01 | A rule's text names a limit its data does not hold ("on work nights", but the rule has no days), so the model guesses whether it applies | EV-03: 1 of 10 picked A because of it; 2 more reasons hedged on it | FR-05 | B6: rules get days, and the text is written from the data. Until then, rule text must not say more than the rule |
| FM-02 | "In use" is any minute used on any day in the window. In irregular homes that covers most of the day, so no gap is left to fix (a TV site in use 1,412 of 1,440 minutes) | REFIT, 3 houses: 0 of 55 runs get a fix; with use on 2 days, 1; on 3 days, 3 | FR-15 | Use needs a minute used on several days. Not enough alone: see FM-03 |
| FM-03 | Most "unusual runs" in other homes are real use at an odd hour (a TV at 09:22, a computer on a Saturday afternoon), not power left on. A timer cannot fix those, and should not | REFIT: most of the 55 runs | FR-09, FR-15 | Tell "on at an odd hour" from "left on": e.g. power near standby for hours. Then propose only for the second |
| FM-04 | A must-run device (fridge, freezer) raises alerts from its own cycles | REFIT house 1 fridge: 3.8 a week, fixed and learned | FR-09 | Device type says must run (B6 and the ontology link): no alerts, no fixes |

## Public homes (B3b)

The rules were tuned on one home. Here they run on 3 public homes, 27 appliances with their own meter, 1 to 28 September 2014: REFIT Electrical Load Measurements (Cleaned), Zenodo record 5063428, CC BY 4.0. Script: `evals/refit.py` (it downloads nothing; the CSVs go in the gitignored `data/refit/`). Channel names from NILMTK's REFIT metadata.

| | Fixed values | Learned values |
|---|---|---|
| Unusual runs a week, all 27 appliances | 13.6 | 11.9 |
| Runs with a fix offered | 0 of 55 | 0 of 48 |

What the numbers say:

- **Learning cuts false alarms on big machines.** A dishwasher at 2,272 W active gets `more_w` of about 1,100 W, so its normal cycles stop being unusual (1.8 a week to 0). Same for washing machines. One went up (house 11 washing machine, 0.2 to 1.0 a week): its learned `small_w` of 1 W counts a 2 W standby left on.
- **Two levels is too few for some machines.** A washing machine gets "standby" 87 W: that is a wash phase, not standby. A dishwasher gets 130 W. The values still work, but the levels are not what a person would call them.
- **No fixes at all, with either set of values.** The Responder's fix (a timer over the quiet gap that holds left-on power) fits Desk 2, a very regular desk. In these homes, FM-02 and FM-03 below stop it. This is the main finding: the waste the Responder can fix today is narrow.
- **Fridges and freezers raise alerts but get no fix** (FM-04). No fix is right; the alerts are noise.

| House | Appliance | Levels (standby, active W) | Runs a week: fixed, learned | Runs with a fix: fixed, learned |
|---|---|---|---|---|
| 1 | Fridge | one level | 3.8, 3.8 | 0, 0 |
| 1 | Freezer (1) | one level | 0.0, 0.0 | 0, 0 |
| 1 | Freezer (2) | one level | 0.0, 0.0 | 0, 0 |
| 1 | Washer dryer | 2, 1322.25 | 0.0, 0.0 | 0, 0 |
| 1 | Washing machine | 86.8, 1144.55 | 0.5, 0.0 | 0, 0 |
| 1 | Dishwasher | 1, 2272.25 | 1.8, 0.0 | 0, 0 |
| 1 | Computer | 1, 26.8571 | 3.0, 3.0 | 0, 0 |
| 1 | TV site | one level | 1.2, 1.2 | 0, 0 |
| 1 | Electric heater | one level | 0.0, 0.0 | 0, 0 |
| 4 | Fridge | 7.1, 51.1429 | 0.0, 0.0 | 0, 0 |
| 4 | Freezer | one level | 0.0, 0.0 | 0, 0 |
| 4 | Fridge-freezer | one level | 0.2, 0.2 | 0, 0 |
| 4 | Tumble dryer | 4.33333, 399.429 | 0.0, 0.0 | 0, 0 |
| 4 | Washing machine (1) | 0.909091, 147.05 | 0.0, 0.0 | 0, 0 |
| 4 | Washing machine (2) | 11, 81.2 | 0.0, 0.0 | 0, 0 |
| 4 | TV site | 10, 77.8889 | 1.5, 1.5 | 0, 0 |
| 4 | Microwave | 8.4, 128 | 0.0, 0.0 | 0, 0 |
| 4 | Kettle | 4.57143, 1717.7 | 0.0, 0.0 | 0, 0 |
| 11 | Fridge | one level | 0.0, 0.0 | 0, 0 |
| 11 | Fridge-freezer | one level | 0.2, 0.2 | 0, 0 |
| 11 | Washing machine | 2, 87.3 | 0.2, 1.0 | 0, 0 |
| 11 | Dishwasher | 130.395, 2887.27 | 0.2, 0.0 | 0, 0 |
| 11 | Computer site | 8, 31 | 0.2, 0.2 | 0, 0 |
| 11 | Microwave | one level | 0.0, 0.0 | 0, 0 |
| 11 | Kettle | 8.1, 1687.7 | 0.0, 0.0 | 0, 0 |
| 11 | Router | one level | 0.0, 0.0 | 0, 0 |
| 11 | Hi-fi | one level | 0.8, 0.8 | 0, 0 |

## Old vs new
Same inputs, AgentC vs rebuild: tokens, cost, latency per run and per month.
