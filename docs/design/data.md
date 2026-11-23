# Data

> Version 1 detail, still true for the built parts. Updated for [design.md](../design.md) version 2 in Phase 3.

[Design overview](../design.md)


Two SQLite files on the hub's SSD, copied to an SD card every night. `energy.db` holds the readings and belongs to the logger. `app.db` holds everything the app knows: plugs, devices, rules, events, proposals and the audit trail. Times are stored in UTC; profiles and timers use the site's local time.

```mermaid
erDiagram
  plugs ||--o{ placements : ""
  devices ||--o{ placements : ""
  devices ||--o{ rules : ""
  plugs ||--o{ timers : ""
  plugs ||--o{ profiles : ""
  events ||--o| proposals : ""
  proposals ||--o{ writes : ""
  proposals ||--o{ llm_calls : ""
```

## energy.db (logger)

| Table | Holds |
|---|---|
| `readings` | One row per plug per minute: `ts_utc`, `plug` (name), power, apparent and reactive power, power factor, voltage, current, kWh counter, `error` when the read failed. Exists and runs today |
| `readings_15m` | 15-minute averages, added by the logger's daily rollup |

Retention: raw minutes for 30 days; then the daily rollup writes 15-minute rows and moves the raw rows to a monthly compressed CSV on the SSD, kept for forecast work (NFR-08 storage). The logger re-reads `config.toml` when the file changes, so a new plug needs no restart.

## app.db (app)

Every table also has `site_id`.

| Table | Holds | Notes |
|---|---|---|
| `sites` | Name, time zone | |
| `plugs` | MAC (key), name, IP, first and last seen | Name links to `readings.plug`; IP may change |
| `timers` | Plug, slot 1 to 16, the Tasmota fields, source (import, actuator, owner), read at | Last read from the plug, not a plan |
| `profiles` | Plug, hour of week (0 to 167), median W, share of time on, days used | Rebuilt daily |
| `thresholds` | Plug, name, value, why | Values learned from each plug's readings, e.g. its standby level |
| `events` | Kind, plug, start, facts, status (new, done, skipped, parked), tries, dedupe key | Dedupe key: plug + kind + day. Skipped: code needed no look (a habit, or drift not a pattern yet) |
| `cards` | Plug, kind, room, what is plugged in, notes, must run, why each timer is set | The house model's card (FR-30) |
| `automations` | Plug, auto-off seconds, plug rules | Read with the timers |
| `habits` | Plug, text, days, start, end, source (tap, chat, setup), ended | A habit that covers a signal stops the look (FR-33) |
| `questions` | Event, plug, ask or propose, text, choices, status (open, answered, expired), when it may reach the owner, the answer | Quiet hours and the daily limit set "when" (FR-36). Expires 12 h after it is due |
| `audit` | Time, actor (code, agent, owner), action, reference, detail | Append only; the history page reads it |
| `llm_calls` | Run, agent, model, prompt version, prompt, output, tokens, USD, latency | Trace and budget |
| `settings` | When each job last ran | Key and value |

Version 1 also had `devices`, `placements`, `rules`, `weather`, `factors`, `goals`, `proposals` and `writes`. They were never used by version 2 and were dropped in Phase 3d (schema 8); Phase 4 adds back what the Actuator needs. Device names, access and rules live in `config.toml`; the grid factor too.

## Proposal contract (FR-14, version 1)

```json
{
  "id": "p-0142", "version": 1, "source": "responder", "event_id": "e-0388",
  "device": "Desk 2",
  "changes": [{"slot": 9, "action": "off", "time": "01:30", "days": "SMTWTFS"}],
  "reason": "Desk 2 stayed on until 06:00 on 2 of the last 11 nights; on other nights it is off by 01:00.",
  "savings_week": {"kwh": 0.06, "lkr": 3, "kgco2e": 0.03},
  "factors": ["grid-2025", "tariff-2026-11"],
  "evidence": ["2 nights on until 06:00 vs profile", "usage until 00:50 on 2 nights"],
  "warnings": [],
  "expires": "2026-11-14T01:30:00+05:30"
}
```

Savings in the example are illustrative, sized from the real readings. The Responder that wrote these was removed in Phase 3c: the housemate's asks and proposals go to `questions`.

## Tasmota timers

A plug has 16 timers. The app reads them all with `Timers` and writes one with `TimerN` plus a JSON value.

| Tasmota field | Values | Our use |
|---|---|---|
| `Enable` | 0, 1 | 1 when written |
| `Mode` | 0 time, 1 sunrise, 2 sunset | Agents write 0 only; imported 1 and 2 are kept and shown |
| `Time` | `HH:MM`, local time on the plug | From the proposal |
| `Window` | 0 to 15 min random offset | 0 |
| `Days` | 7 characters, Sunday first; `1` or the day letter = on, `0` or `-` = off | From the proposal |
| `Repeat` | 0 once, 1 weekly | 1 |
| `Output` | Relay number | 1 |
| `Action` | 0 off, 1 on, 2 toggle, 3 rule | 0 or 1 only |

- **Slots:** the Actuator writes only slots listed for the device (default 9 to 16). Slots 1 to 8 are imported as the owner's and never changed.
- **Global switch:** timers fire only when the plug's `Timers` setting is on. Import checks it; if it is off, that becomes a setup question, not a silent write.
- **Clock:** plugs must keep local time (A-02). Import reads `Time` and `Timezone` and raises a setup question when they are wrong.
- **Read-back:** after a write the Actuator reads the same slot and compares every field.
