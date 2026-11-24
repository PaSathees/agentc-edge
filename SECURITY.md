# Security

## Report a problem

Please open a [private security advisory](https://github.com/PaSathees/agentc-edge/security/advisories/new) on GitHub, not a public issue. I will answer within a week.

## What it protects, and what it does not

- **It runs at home.** The hub reads your plugs over your home network. No port is opened to the internet. To answer away from home, use Tailscale, a private network between your own devices.
- **The pages need your login.** The inbox and the setup chat open only for you: your Tailscale login through Tailscale Serve (`AGENTC_TAILSCALE_USER`), or a password (`AGENTC_PASSWORD`, a signed cookie for 30 days). The Tailscale header counts only on a request from the hub itself. With neither set, the pages are open to your network and nothing can be written to a plug.
- **Only the hub's own names are answered.** A request for any other host name is refused, which blocks DNS rebinding (a web page that renames itself to reach the hub through your browser).
- **ntfy is open to your network.** Anyone on it can read your messages if they learn the topic, which is made from the hub's secret, so it is hard to guess.
- **Buttons are signed.** Each answer needs a token signed with the hub's secret (`data/secret`). Once a question is answered, its token does nothing.
- **What leaves the hub:** the housemate's prompts to the Anthropic API (no IP or MAC addresses), and on iPhone a message id that ntfy.sh relays to wake the app. Your readings stay on the hub.
- **A plug changes only after your tap**, only for a device you set to `write`, and only with shadow mode off and a login set. At most 4 changes a plug a day; each one is read back and kept in the history. A switch off can be undone for a day.
- **The Tailscale name is trusted from the hub itself.** Tailscale Serve runs on the hub, so a request from 127.0.0.1 with a `Tailscale-User-Login` header counts as that user. Never put another proxy in front of the app on the hub: it would be trusted too.
