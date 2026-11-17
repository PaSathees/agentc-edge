# Eval data

Inputs and recorded answers for the agent evals. The cases and scorers are in `src/agentc/evals.py`; the plan is in [docs/evaluation.md](../docs/evaluation.md).

- `desk2.csv.gz`: Desk 2's real readings, one a minute, 1 to 15 Nov 2026 (Asia/Colombo). Only times (UTC) and watts: no plug name, MAC, IP or place (NFR-14).
- `recorded.json`: the real model's answers from the last live run, keyed by a hash of what the model reads. Tests replay them on every commit for free.

Run the evals:

```bash
uv run agentc eval
```

Replays the recorded answers. Free. Fails if the prompt, facts or schema changed since the recording.

```bash
uv run --env-file .env agentc eval --live
```

Asks the real model 10 times per case and records the answers. Run it when a prompt, model or tool changes. About USD 0.003.
