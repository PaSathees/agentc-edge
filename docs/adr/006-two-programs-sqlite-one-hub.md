# ADR-006: Two programs, SQLite, one hub

Status: accepted
Date: 2026-11-12
Serves: NFR-07, NFR-08, NFR-09, NFR-10, NFR-15, NFR-20, C-02, C-03

## Context

AgentC ran in the cloud on Railway with Postgres and InfluxDB. The rebuild must run on one laptop inside the site, cost nothing to host (C-03), and keep logging no matter what the rest does.

## Options
1. Cloud services, as before.
2. Docker Compose on the hub with Postgres, a time-series database and separate services.
3. Two programs on the hub: the existing logger and one app process; SQLite files for data.

## Decision

Option 3. The logger writes `energy.db`; the app reads it and owns `app.db`. Both are systemd units. All SQL goes through one `store` module.

## Consequences

- Nothing to pay for and little to run: two processes, two files, within the idle budget (NFR-07).
- Logging survives app crashes and upgrades (NFR-09).
- Backup is copying two files.
- SQLite limits writers. Fine for one site; `store` and `site_id` on every table keep a move to Postgres or Timescale a contained change (NFR-15).
- Everything in one app process means one bad job can slow the rest; jobs are short and the logger is outside it.
