# My hub's notes

How I set up the operating system of my own hub (an HP ProBook, Debian, always on). You do
not need these to run AgentC: [docs/setup.md](../docs/setup.md) is all you need.

- `hub-setup.sh`: fewer disk writes, never sleep, SSH keys only, boot quirks of this laptop
- `hub-data.sh`: the `/data` partition
- `energy-backup.*`, `99-backup-card.rules`: a nightly copy of the readings to an SD card

The app itself runs with `docker compose up -d`, with `AGENTC_DATA_DIR=/data` in `.env`.
