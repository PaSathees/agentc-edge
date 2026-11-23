# Setup

Six steps, about 20 minutes. Everything runs in Docker on one always-on computer at home.

```
smart plugs (Wi-Fi) ──► this box: docker compose ──► your phone (ntfy app)
                         logger · jobs · inbox · ntfy
```

## What you need

- **Smart plugs running [Tasmota](https://tasmota.github.io/docs/) with an energy sensor**, on your home Wi-Fi. A plug fits if its web page shows watts. Give each one a fixed address in your router, so it does not move.
- **An always-on computer on the same network, with Docker**: an old laptop, a Raspberry Pi 4 or 5, or a mini PC running Linux. Install Docker with your system's packages (Debian or Ubuntu: `sudo apt install docker.io docker-compose`), then add yourself to the `docker` group and log in again.
- **The [ntfy](https://ntfy.sh/) app** on your phone (App Store, Google Play or F-Droid).
- **Optional: an [Anthropic API key](https://console.anthropic.com/).** Without it you still get readings, signals and health notices; with it, the housemate looks into each signal and asks you short questions. It is capped at USD 3 a month by default; one home used about USD 0.001 a look.

## 1. Get the code

```bash
git clone https://github.com/PaSathees/agentc-edge.git && cd agentc-edge
```

## 2. Make your config

```bash
cp config.example.toml config.toml
```

Open `config.toml`. Set your site's `name` and `tz` (time zone, e.g. `Europe/London`). Leave the rest for now.

## 3. Find your plugs

```bash
docker compose run --rm jobs find
```

It asks each address on your home network whether it is a Tasmota plug. It only reads, and changes nothing. Paste the lines it prints under `[plugs]` in `config.toml`, in place of the two examples. Rename them if you like (`"Desk" = "192.168.1.40"`).

## 4. Make your `.env`

`find` also printed this box's address. Put it in a file called `.env`, with your API key if you have one:

```bash
AGENTC_HUB=http://192.168.1.50
ANTHROPIC_API_KEY=sk-ant-...
```

If `id -u` does not print `1000`, add `AGENTC_UID=` and `AGENTC_GID=` with your own numbers, so the files in `./data` stay yours.

## 5. Start

```bash
docker compose up -d
```

This starts four services:

| Service | Does |
|---|---|
| `logger` | Reads every plug once a minute into `data/energy.db` |
| `jobs` | Every minute: signals, the housemate, messages. Learns each plug's usual week daily |
| `web` | The inbox at `http://<this box>:8765` |
| `ntfy` | Sends messages to your phone |

## 6. Connect your phone

```bash
docker compose exec jobs agentc ping
```

It prints the ntfy server and topic. In the ntfy app, add that server, subscribe to that topic, and wait for the test message. Tap **Tap test**: a "Tap works" message should follow. Your phone must be on the home Wi-Fi.

Done. After about 7 days of readings, each plug has a usual week, and signals begin.

## Every day

| To | Run |
|---|---|
| Tell it what each plug powers (it asks better questions) | `docker compose exec jobs agentc house Plug-1 kind=computer room=study what="monitor and lamp"` |
| See open questions, or answer one | `docker compose exec jobs agentc questions`, or the inbox page |
| Watch what it does | `docker compose logs -f jobs` |
| Update | `git pull && docker compose up -d --build` |
| Stop | `docker compose down` (readings stop too) |

Back up the `data` folder: it holds every reading (`energy.db`), the house model and your answers (`app.db`), and the key that signs the phone's buttons (`secret`).

Nothing here switches a plug. This version only watches and asks (shadow mode).

## Away from home: Tailscale (optional)

To get and answer messages outside your home Wi-Fi, use [Tailscale](https://tailscale.com/), a private network between your own devices. Install it on this box and on your phone, turn on HTTPS in the Tailscale admin page, then:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8090
```

Set `AGENTC_HUB=https://<this box's name>.<your tailnet>.ts.net` in `.env`, run `docker compose up -d`, then `agentc ping` again and add the new server in the ntfy app. No port is opened to the internet.

## Who can do what

- Anyone on your home network (or your tailnet) can open the inbox page. Answers need a button's signed token, so a guessed address does nothing.
- On iPhone, ntfy.sh only relays a message id to wake the app; the text comes from this box.
- The housemate's prompts never contain IP or MAC addresses.

## If something is wrong

| Problem | Try |
|---|---|
| `find` finds nothing | The plugs and this box must be on the same network. On a Mac or Windows with Docker Desktop, turn on host networking, or give the network: `find 192.168.1.0/24` |
| No messages | `AGENTC_HUB` in `.env`, then `docker compose up -d`; `agentc ping` again |
| No signals after a week | `docker compose logs logger`: every plug should be `ok` |
| Anything else | `docker compose logs`, then open an issue with what it printed |
