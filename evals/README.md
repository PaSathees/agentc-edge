# Eval data

Inputs and recorded answers for the agent evals. The cases and scorers are in `src/agentc/evals.py`; the plan is in [docs/evaluation.md](../docs/evaluation.md).

- `desk2.csv.gz`, `tv.csv.gz`, `box.csv.gz`: real readings of three plugs in the reference home (a desk, the TV, the TV box), one a minute, 1 to 15 Nov 2026 (Asia/Colombo). Only times (UTC) and watts: no plug name, MAC, IP or place (NFR-14: no private data in the repo). They still show when the home used these devices; they are published on purpose, so the evals run on real nights.
- `recorded.json`: every step the real model sent in the last live run, keyed by a hash of what the model reads first (prompt, tools, brief). Tests replay them on every commit for free.

Run the evals:

```bash
uv run agentc eval
```

Replays the recorded steps. Free. Fails if the prompt, the tools or the brief changed since the recording.

```bash
uv run --env-file .env agentc eval --live
```

Lets the housemate look at each case 5 times on the real model and records every step. Run it when a prompt, model or tool changes. About USD 0.01.
