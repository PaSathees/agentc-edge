# Process

Five stages, in order: specify, design, build, evaluate, operate. Soft gates: a phase may be left with unmet exit criteria only with a reason in the gate log. Ported from `agent-sdlc` on 2026-11-08.

## IDs
| Prefix | What | Lives in |
|---|---|---|
| J-xx | Job story | `requirements.md` |
| FR-xx | Functional requirement, `[M]` must or `[S]` should, with AC | `requirements.md` |
| NFR-xx | Non-functional requirement, with a number | `requirements.md` |
| SM-xx | Success measure | `requirements.md` |
| C-xx, A-xx | Constraint, assumption | `requirements.md` |
| ADR-xxx | Decision record | `adr/` |
| EV-xx | Eval case | planned in `evaluation.md`, data in `evals/` |
| FM-xx | Failure mode from error analysis | `evaluation.md` |

- Every item cites the ids it serves, in brackets: `(FR-03)`.
- Ids are never reused or renumbered. A dropped item stays, struck through, with the reason.

## Stages
| Stage | Artifacts | Exit criteria |
|---|---|---|
| 1 Specify | `audit.md` (rebuild only), `requirements.md` | Every J has an FR; every FR has a priority and AC; every NFR has a number; cost target is an SM; open questions listed |
| 2 Design | `design.md`, `adr/` | Every FR is covered by the design; every step says who does it (code, AI or the owner); each AI step has a model, budget, guardrails and eval plan; one ADR per big call; EV cases v0 written |
| 3 Build | code, tests, `evals/` | Every AC of an `[M]` FR has a test; evals run in CI; vertical slice demo |
| 4 Evaluate | `evaluation.md` | Every LLM FR scored against its threshold; one error analysis round; old vs new cost measured |
| 5 Operate | `operations.md` | Shadow mode run; traces reviewed weekly; drift checked; retrospective written |

## Build in phases
- Version 1 was built in slices B0 to B3b (skeleton, read, watch, propose). After a review it was redesigned (design version 2), and the build continues in phases: 1 design v2, 2 foundation fixes, 3 housemate v1, 4 actions, 5 chat and Planner, 6 review of the whole system, then the full demo. See the README roadmap.
- Per phase: brief (ACs in scope, files, tests) → failing tests from the ACs and EV cases → code → the code broken on purpose once, to prove the tests catch it → ruff, types, secrets scan, CI on a clean copy → shown on real data, read-only → one pull request.
- One phase open at a time.

## AI-assisted development
- An AI coding assistant (Claude Code) writes most of the code and tests and drafts the docs. The owner writes the brief, makes every design decision, checks results on real data, and reviews and merges every change.
- AI-written code meets the same bar as any code: the steps above, every time.
- Commits record it with a trailer in the [Linux kernel's format](https://docs.kernel.org/process/coding-assistants.html): `Assisted-by: AGENT:MODEL`, for example `Assisted-by: Claude:claude-opus-5-5`. `Co-authored-by` is kept for people.
- The owner must be able to explain any line that is merged.

## Rules for agent parts
- Code computes every number and makes every control decision. The LLM drafts, explains and proposes.
- No LLM step ships without an eval.
- Every write to a device needs a proposal, an approval and an audit row.

## Gate log
| Date | Leaving | Unmet criteria | Reason |
|---|---|---|---|
| 2026-11-11 | 1 Specify | none | Passed; 6 open questions carried into Design |
| 2026-11-12 | 2 Design | none | Passed; 25 D steps, 16 M modules, 6 ADRs, 21 EV cases; open questions answered |
| 2026-11-21 | 2 Design, version 2 | none | Passed after the owner's review walkthrough: agents on a shared house model with read tools (ADR-007), switch off now (ADR-008), own agent loop (ADR-009); requirements v2; 9 review changes and 3 owner calls applied |
