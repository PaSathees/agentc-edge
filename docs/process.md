# Process

Five phases, in order: specify, design, build, evaluate, operate. Soft gates: a phase may be left with unmet exit criteria only with a reason in the gate log. Ported from `agent-sdlc` on 2026-11-08.

## IDs
| Prefix | What | Lives in |
|---|---|---|
| J-xx | Job story | `requirements.md` |
| FR-xx | Functional requirement, `[M]` must or `[S]` should, with AC | `requirements.md` |
| NFR-xx | Non-functional requirement, with a number | `requirements.md` |
| SM-xx | Success measure | `requirements.md` |
| C-xx, A-xx | Constraint, assumption | `requirements.md` |
| D-xx | Design step (autonomy map) | `design.md` |
| M-xx | Module (architecture) | `design.md` |
| ADR-xxx | Decision record | `adr/` |
| EV-xx | Eval case | planned in `evaluation.md`, data in `evals/` |
| FM-xx | Failure mode from error analysis | `evaluation.md` |

- Every item cites the ids it serves, in brackets: `(FR-03)`.
- Ids are never reused or renumbered. A dropped item stays, struck through, with the reason.

## Phases
| Phase | Artifacts | Exit criteria |
|---|---|---|
| 1 Specify | `audit.md` (rebuild only), `requirements.md` | Every J has an FR; every FR has a priority and AC; every NFR has a number; cost target is an SM; open questions listed |
| 2 Design | `design.md` (overview), `design/`, `adr/` | Every FR has a D item; the autonomy map covers every step; each LLM step has a model, budget, guardrails and eval plan; one ADR per big call; EV cases v0 written |
| 3 Build | code, tests, `evals/` | Every AC of an `[M]` FR has a test; evals run in CI; vertical slice demo |
| 4 Evaluate | `evaluation.md` | Every LLM FR scored against its threshold; one error analysis round; old vs new cost measured |
| 5 Operate | `operations.md` | Shadow mode run; traces reviewed weekly; drift checked; retrospective written |

## Rules for agent parts
- Code computes every number and makes every control decision. The LLM drafts, explains and proposes.
- No LLM step ships without an eval.
- Every write to a device needs a proposal, an approval and an audit row.

## Gate log
| Date | Leaving | Unmet criteria | Reason |
|---|---|---|---|
| 2026-11-11 | 1 Specify | none | Passed; 6 open questions carried into Design |
| 2026-11-12 | 2 Design | none | Passed; 25 D steps, 16 M modules, 6 ADRs, 21 EV cases; open questions answered (`design/open-questions.md`) |
