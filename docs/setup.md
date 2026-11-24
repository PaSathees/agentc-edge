# Setup

Six steps, about 20 minutes. Everything runs in Docker on one always-on computer at home: the hub.

```
smart plugs (Wi-Fi) ──► the hub: docker compose ──► your phone (ntfy app)
                         logger · jobs · inbox · ntfy
```

## What you need

- **Smart plugs running [Tasmota](https://tasmota.github.io/docs/) with an energy sensor**, on your home Wi-Fi. A plug fits if its web page shows watts. Give each one a fixed address in your router, so it does not move.
- **An always-on computer on the same network (the hub), with Docker and Compose 2.24 or newer**: an old laptop, a Raspberry Pi 4 or 5, or a mini PC running Linux. Install them with [Docker's guide](https://docs.docker.com/engine/install/), or your system's packages if they are new enough (Debian 13: `sudo apt install docker.io docker-compose`). Check with `docker compose version`. Then add yourself to the `docker` group and log in again.
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

```bash
cp .env.example .env
```

Fill in `AGENTC_HUB` with the hub's address, which `find` printed (`AGENTC_HUB=http://192.168.1.50`), and your API key if you have one. Set a password in `AGENTC_PASSWORD` (`openssl rand -base64 18` makes one), so only you can open the pages. If `id -u` does not print `1000`, also set `AGENTC_UID` and `AGENTC_GID` to your own numbers, so the files in `./data` stay yours.

## 5. Start

```bash
docker compose up -d
```

This starts four services:

| Service | Does |
|---|---|
| `logger` | Reads every plug once a minute into `data/energy.db` |
| `jobs` | Every minute: signals, the housemate, messages. Learns each plug's usual week daily |
| `web` | The inbox at `http://<the hub>:8765` |
| `ntfy` | Sends messages to your phone |

## 6. Connect your phone

```bash
docker compose exec jobs agentc ping
```

It prints the ntfy server and topic. In the ntfy app, add that server, subscribe to that topic, and wait for the test message. Tap **Tap test**: a "Tap works" message should follow. Your phone must be on the home Wi-Fi.

Done. After about 7 days of readings, each plug has a usual week, and signals begin.

## 7. Tell it about your devices

Open `http://<the hub>:8765/setup` (Devices, at the top of every page). For each plug, press **Set up** and tell it what is plugged in, in your own words. It looks at the plug's readings, asks a few short questions, and shows a card. Nothing is saved until you press **Save**. Each chat costs about USD 0.002 on Claude Haiku (the default small model); without one, set cards by hand: `docker compose exec jobs agentc house Plug-1 kind=computer room=study`.

## 8. Let it switch a device (optional)

At first AgentC only asks and records your answers (shadow mode). To let a tap on "Switch off" really switch a device:

1. In `config.toml`, set that device's `access = "write"`. Start with one that is safe to switch off, like a TV box.
2. Set a login in `.env` (`AGENTC_PASSWORD`, or `AGENTC_TAILSCALE_USER`), then `docker compose up -d`.
3. On the Devices page, tap **Turn shadow mode off**.

Each switch off is read back from the plug and kept in the history. For a day, the inbox and the phone's "Done" message have a **Switch back on** button. A plug is changed at most 4 times a day, and a device that must always run (a fridge, a router) is never switched off.

## Every day

| To | Run |
|---|---|
| Tell it what each plug powers (it asks better questions) | The Devices page, `http://<the hub>:8765/setup`, or `agentc house Plug-1 kind=computer` |
| See open questions, or answer one | `docker compose exec jobs agentc questions`, or the inbox page |
| Watch what it does | `docker compose logs -f jobs` |
| Update | `git pull && docker compose up -d --build` |
| Stop | `docker compose down` (readings stop too) |

Back up the `data` folder: it holds every reading (`energy.db`), the house model and your answers (`app.db`), and the key that signs the phone's buttons (`secret`).

Nothing here switches a plug. This version only watches and asks (shadow mode).

## Away from home: Tailscale (optional)

To get and answer messages outside your home Wi-Fi, use [Tailscale](https://tailscale.com/), a private network between your own devices. Install it on the hub and on your phone, and sign in to both. Then put the hub's Tailscale address in `.env`:

```bash
AGENTC_HUB=http://100.x.y.z        # from `tailscale ip -4` on the hub
```

Run `docker compose up -d`, then `agentc ping` again. In the ntfy app, subscribe on the new server it prints, and delete the old subscription and its messages: an old message's buttons still point at the old address.

- **Keep Tailscale on, at home too.** The phone reaches the hub only through it now. At home Tailscale goes straight over your Wi-Fi, so nothing leaves the house. On iPhone, turn VPN On Demand off, or include your home Wi-Fi in it.
- **No HTTPS or `tailscale serve` is needed for messages.** Tailscale already encrypts the traffic between your devices, and no port is opened to the internet.
- **To talk instead of type** in the setup chat, browsers need https. Turn on HTTPS in the Tailscale admin page (DNS tab), run `sudo tailscale serve --bg --https=443 http://127.0.0.1:8765` and `sudo tailscale serve --bg --https=8443 http://127.0.0.1:8090` on the hub, and set `AGENTC_HUB=https://<the hub>.<your tailnet>.ts.net`. Then `agentc ping` again and add the new ntfy server.
- **Sign in with Tailscale, no password.** Set `AGENTC_TAILSCALE_USER` in `.env` to your Tailscale login (`tailscale status --json | grep LoginName` on the hub). Pages opened through `tailscale serve` then know it is you.
- After the phone changes network (Wi-Fi to mobile data), the first page or tap can take a few seconds while Tailscale reconnects.

## Who can do what

- **The pages need your login**: your Tailscale login or your password. With neither set in `.env`, they are open to your network, and nothing can be written to a plug.
- **ntfy is open to your network.** Anyone on it can read your messages if they learn the topic, which is made from the hub's secret, so it is hard to guess.
- A button's address only works with its signed token: a guessed address does nothing.
- On iPhone, ntfy.sh only relays a message id to wake the app; the text comes from the hub.
- The housemate's prompts never contain IP or MAC addresses. ntfy never sees your API key.
- Nothing switches a plug yet (shadow mode). Phase 4 adds writes, after your tap, only to devices you set to `write`.

## If something is wrong

| Problem | Try |
|---|---|
| `find` finds nothing | The plugs and the hub must be on the same network. On a Mac or Windows with Docker Desktop, turn on host networking, or give the network: `find 192.168.1.0/24` |
| No messages | `AGENTC_HUB` in `.env`, then `docker compose up -d`; `agentc ping` again |
| No signals after a week | `docker compose logs logger`: every plug should be `ok` |
| Anything else | `docker compose logs`, then open an issue with what it printed |
