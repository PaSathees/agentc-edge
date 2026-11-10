# AgentC Edge

Edge agents for energy and carbon operations. Code watches the meters, agents wake only when something happens, and a person approves every change before it reaches a device.

> Status: in development. Built in the open with a documented process (`docs/`).

## How it works

```mermaid
flowchart LR
  P["Smart plugs<br/>run their own timers"] --> L["Logger"] --> DB[("SQLite")]
  DB --> W["Watcher<br/>code"]
  W -- "event" --> A["Agents<br/>Responder · Planner · Assistant"]
  A -- "proposal" --> I["Approval inbox"]
  I -- "approved" --> X["Actuator<br/>code"] --> P
```

- **Event-driven:** no daily LLM run. LLM cost follows events, not devices.
- **Human in the loop:** every change is a proposal; nothing is written without approval. Shadow mode first.
- **Code does the maths:** kWh, cost and kgCO2e are computed in code; agents explain and propose.
- **Runs on the edge:** one laptop inside the site; only model API calls leave the box.

## Quickstart

```bash
cp config.example.toml config.toml   # add your plugs
python3 logger/logger.py energy.db config.toml
```

## Results

Not measured yet. See `docs/evaluation.md`.

## Engineering process

Five phases (specify, design, build, evaluate, operate), each with its artifacts:

| Phase | Artifact |
|---|---|
| Specify | [requirements](docs/requirements.md), [audit of v1](docs/audit.md) |
| Design | [design](docs/design.md), [ADRs](docs/adr/) |
| Evaluate | [evaluation](docs/evaluation.md) |
| Operate | [operations](docs/operations.md) |

Process and conventions: [docs/process.md](docs/process.md).

## Repository layout

```
logger/   plug logger (stdlib only)
deploy/   hub setup scripts and systemd units
docs/     requirements, design, ADRs, evaluation, operations
```

## Limits

Home smart plugs are not industrial data. Industrial protocols and the grid are simulated where used.

## License

Apache-2.0. See [LICENSE](LICENSE).
