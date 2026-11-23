# Security

## Report a problem

Please open a [private security advisory](https://github.com/PaSathees/agentc-edge/security/advisories/new) on GitHub, not a public issue. I will answer within a week.

## What it protects, and what it does not

- **It runs at home.** The hub reads your plugs over your home network. No port is opened to the internet. To answer away from home, use Tailscale, a private network between your own devices.
- **Your network is trusted.** Anyone on your home network, or your tailnet, can open the inbox page and answer questions. They can read your messages in ntfy if they learn its topic, which is made from the hub's secret, so it is hard to guess. Keep the hub on a network you trust.
- **Buttons are signed.** Each answer needs a token signed with the hub's secret (`data/secret`). Once a question is answered, its token does nothing.
- **What leaves the hub:** the housemate's prompts to the Anthropic API (no IP or MAC addresses), and on iPhone a message id that ntfy.sh relays to wake the app. Your readings stay on the hub.
- **Nothing switches a plug yet** (shadow mode). Before Phase 4 lets it, the inbox and ntfy get a login.
