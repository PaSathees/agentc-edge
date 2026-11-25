# AgentC Edge

[![CI](https://github.com/PaSathees/agentc-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/PaSathees/agentc-edge/actions/workflows/ci.yml)

Helps a small site meet a monthly carbon goal, starting with a home. The site's carbon comes from the electricity it uses, so plain code watches every smart plug all the time. When something looks off, an AI agent takes a look with what it knows about the home's devices and habits, and asks the owner a short question while it is happening. A person approves every change before it reaches a device.

> Status: in development, running in one home. It watches and asks; it switches a plug only after the owner's tap, and only a device the owner set to `write` (shadow mode, on by default, records instead). Built in the open, step by step (`docs/`). Words and codes are explained in the [glossary](docs/glossary.md).

## How it works

```mermaid
flowchart LR
  P["Smart plugs<br/>run their own timers"] --> L["Logger"] --> DB[("Database")]
  DB --> S["Signals<br/>code"]
  S -- "signal" --> A["Housemate<br/>AI agent"]
  A -- "question" --> O["Your phone<br/>and inbox"]
  O -. "approved change (Phase 4)" .-> X["Actuator<br/>code"] -.-> P
```

- **AI only when needed:** plain code checks the readings every few minutes. The AI runs only when it finds something, so a quiet day costs nothing.
- **A person decides:** every change is a proposal. Nothing reaches a plug without approval. It starts in shadow mode: it suggests, but writes nothing.
- **Code does the maths:** kWh, cost and carbon are worked out in code. The AI explains and suggests.
- **Runs at the site:** one old laptop. Only the AI calls leave it, and on iPhone a message id that wakes the ntfy app.

## Commands

| Command | Does |
|---|---|
| `agentc find` | Finds the Tasmota plugs on your home network and prints their config lines. Reads only |
| `agentc ping` | Sends a test message to your phone, with a tap test |
| `agentc run` | The jobs service: the due jobs every minute, for good. `agentc log` is the logger service |
| `agentc timers` | Reads every plug and shows its timers in plain words. Also reads what each plug does on its own (auto-off, rules) for the house model. Problems come last |
| `agentc profile Plug-6` | Shows a plug's usual week, hour by hour, and the kWh it used |
| `agentc watch` | Runs the checks that are due now and shows new signals. With an API key, the housemate looks at each one and decides: ignore, note or ask. With `AGENTC_HUB` in `.env`, due questions go to your phone through ntfy: the night's as one message at 07:00, the day's notes at 21:00 |
| `agentc questions` | The housemate's open questions, numbered, with when each one reaches you (07:00 for a small load at night) |
| `agentc answer 3 2` | Answers question 3 with choice 2, as a tap would. "That's normal" saves a habit; a switch off goes through the same checks as a tap |
| `agentc look 12 --at "2026-11-02 02:10"` | Runs the housemate on one saved signal and shows the tools it used and its decision |
| `agentc replay --days 14 [--look]` | Plays past days through the watch pass, and the housemate with `--look`. Your real data is not changed |
| `agentc eval [--live]` | Scores the housemate on real nights: replays recorded steps free, or asks the model again. From the repo with `uv run` (see Develop), not in Docker |
| `agentc learn` | Learns each plug's usual week and standby level now (the jobs do it daily) |
| `agentc policy [Plug-1]` | Every threshold: its value, where it came from, and why |
| Devices page (`/setup` in the inbox) | Set up each device by chatting: it reads the plug, asks a few questions, shows a card; only your Save writes it. Talk instead of type on https |
| `agentc house` | Shows what the system knows about each device: its card, timers and habits, as the agent will read it |
| `agentc house Plug-6 kind=computer room=study` | Changes a device's card. Keys: kind, room (where it is), what, notes, must_run, why.N (why timer N is set) |

In Docker, run them as `docker compose exec jobs agentc ...`. None of these change a plug. They only read the plugs; answers and cards are saved in app.db.

## Quickstart

Needs Tasmota smart plugs on your Wi-Fi and Docker on an always-on computer. Full guide, with the phone: [docs/setup.md](docs/setup.md).

```bash
git clone https://github.com/PaSathees/agentc-edge.git && cd agentc-edge
cp config.example.toml config.toml        # set your site name and time zone
docker compose run --rm jobs find         # finds your plugs: paste the lines into config.toml
cp .env.example .env                      # AGENTC_HUB: this box's address (find prints it); your API key if any
docker compose up -d                      # logger, jobs, inbox and ntfy
docker compose exec jobs agentc ping      # connect the ntfy app on your phone
```

## Develop

Needs [uv](https://docs.astral.sh/uv/). Tests use a fake plug, so no hardware is needed.

```bash
uv sync                      # Python 3.12 and the libraries
uv run pytest                # all tests, with coverage
uv run ruff check . && uv run basedpyright   # style and type checks, as in CI
uv run uvicorn agentc.app:app                # the inbox at localhost:8000
uv run python scripts/dev.py --from-hub edge-hub   # the web app on a copy of your hub's data, with fake plugs, at localhost:8770
docker compose up -d --build                 # the whole system, as users run it
```

## Results

Not measured yet. See [evaluation](docs/evaluation.md).

## Roadmap

Built in six phases. Each ends with tests, checks and a merged pull request.

| Phase | What | Status |
|---|---|---|
| 1. Design v2 | The housemate design, after a review of the first version | done |
| 2. Foundation fixes | Logger heartbeat, honest "lost contact" alerts, AI errors that never stop other jobs | done |
| 3. Housemate v1 | Device cards from a setup chat, new signals, the agent with tools, one-tap questions; shadow mode | done; shadow mode counts run to December |
| 4. Actions | A login, switch off now with undo, timer writes all or nothing | done: first real write and undo from the phone on 2026-11-25 |
| 5. Chat and Planner | Separate agents on the same house model | |
| 6. Review and full demo | A walkthrough of the whole system, then the demo on a real home | |

## How it is built

The method has five stages, each with its own documents:

| Stage | Documents |
|---|---|
| Specify: what it must do | [requirements](docs/requirements.md), [review of the first version](docs/audit.md) |
| Design: how it will work | [design](docs/design.md) (version 2), [decision records](docs/adr/) |
| Evaluate: does it work | [evaluation](docs/evaluation.md) |
| Operate: running it | [operations](docs/operations.md) |

The method: [docs/process.md](docs/process.md).

### AI use

This project is built with an AI coding assistant, [Claude Code](https://claude.com/claude-code). What each side does:

- **Me:** the problem and the requirements, every design decision and its trade-offs (the [decision records](docs/adr/)), checks on real data, and the review and merge of every change.
- **Claude Code:** most of the code and tests, and first drafts of the docs, from a brief I agree first.
- **Every change, whoever wrote it:** tests written first and seen failing, the code broken on purpose once to prove the tests catch it, lint, type and secret checks, CI on a clean copy, and a review before merge.

Commits from 2026-11-21 on say so with an `Assisted-by:` line, in the Linux kernel's format (`Assisted-by: Claude:claude-opus-5-5`). Earlier commits were built the same way but don't carry the line.

## Folders

```
src/          the app: the agentc package (the logger is agentc/logger.py, standard library only)
tests/        tests and the fake plug
evals/        real readings and recorded model steps for the evals
docs/         setup, requirements, design, decisions, evaluation, operations
deploy/       notes for my own hub's operating system (not needed to run AgentC)
compose.yaml  the whole system: logger, jobs, inbox, ntfy
data/         your data, made on first start: readings, the house model, answers
```

## Limits

- Home smart plugs are not industrial data. Industrial protocols and the grid are simulated where used.
- A personal project. Not linked to CEB, PUCSL or SLSEA.
- The tariff in `config.example.toml` uses fake rates. Check your own bill.
- It can switch real plugs. Use it at your own risk. Never put it on medical, safety or fire-risk loads.
- Carbon figures are estimates from a published grid factor, not a certified report.

## License

Apache-2.0. See [LICENSE](LICENSE).
