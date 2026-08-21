# Security

## What this project assumes

llm-box is built for a machine you control on a network you trust — a home lab, a
workstation, a server in your own office. It is **not** hardened for the open
internet, and nothing in it should be exposed to one without a layer in front.

Shipped defaults are therefore loopback-only. You have to open it up deliberately.

## What listens where

| Service | Port | Default bind | Authentication |
|---|---|---|---|
| llama-swap (OpenAI-compatible API) | 8080 | `127.0.0.1` | **none** |
| Registry (catalog + MCP) | 8081 | `127.0.0.1` | reads open, writes need a token |
| Open WebUI (chat) | 3000 | `127.0.0.1` | account required (`WEBUI_AUTH=True`) |
| ComfyUI (images) | 8188 | `127.0.0.1` | **none** |

`llama-swap` accepts any `Authorization` header value — the `sk-local` you see in
the examples is a placeholder, not a secret. Anyone who can reach port 8080 can
run any configured model and read any file the server process can read via a
model path. Treat reachability as full access.

Three specifics on port 8080 that are easy to miss:

- **`GET /unload` unloads every model**, with no token and no confirmation. It is
  a *mutating GET*, so it does not take a person: a browser prefetch, a link
  checker, a chat client that unfurls URLs, anything that follows links will
  empty your VRAM. Observed, not theoretical — one automated fetch of that path
  dropped two models that were pinned resident with `ttl: 0`.
- **The web interface at `/ui`** (llama-swap's own, see [docs/UI.md](docs/UI.md))
  comes with a playground and a full log viewer. Whoever reaches the port gets
  free inference and your upstream logs.
- **Reads on the registry, port 8081, need no token** and return every model's
  filesystem path, SHA-256 and Hugging Face repo, plus live VRAM per card.

One specific on port 8081 for the same reason: a write there can now **start a
build and restart a service**. `POST /api/updates/<component>` and
`POST /api/rollback/<component>` run the same `llm update` / `llm rollback` the
CLI does — minutes of CPU, a few seconds of downtime, and a different binary
afterwards. They need the token or a session, like every other write, and the
component is checked against a fixed list rather than a pattern because it
becomes part of a command line. Nothing new is reachable without the token; what
is new is how much one leaked token can do with it.

None of that matters on the shipped `127.0.0.1` default. All of it matters the
moment you set `LLM_BIND=0.0.0.0`. If you need remote access, prefer the SSH
tunnel in [docs/REMOTE.md](docs/REMOTE.md) over opening the ports.

### If you do open port 8080: `llm key`

llama-swap is default-allow, but it can require a key. `llm key new` generates
one, writes it into the configuration and tells you which clients need a nudge:

```bash
llm key new        # generate and wire it in
llm restart        # llama-swap starts enforcing it
llm ui restart     # the chat UI picks it up from config/api-key.env
llm key            # print it
llm key off        # back to open
```

With a key in force, **every path except `/health`** needs
`Authorization: Bearer <key>` — measured: `/v1/models`, `/unload`, `/ui`,
`/logs`, `/metrics` and `/running` all answer 401 without it. That closes the
mutating GET, the open playground and the log viewer in one move, and it does so
without moving the port to loopback, so remote inference keeps working.

Three consequences worth knowing before you turn it on:

- **Every client needs the key.** The registry hands it to pi, so pi picks it up
  on its next refresh; Open WebUI reads it from `config/api-key.env` on restart.
  Anything else you have pointed at `:8080/v1` with a hardcoded key has to be
  updated by hand.
- **`GET /api/pi-models.json` stops being open.** That payload carries the key,
  so once one is set the endpoint requires the registry token — otherwise the
  key would be readable by anyone who can reach port 8081, which would undo the
  whole exercise. `llm api client` prints the line pi needs.
- **The key travels in plain HTTP.** It is a door, not a secret channel. On a
  network you do not control, put TLS in front (llama-swap takes
  `-tls-cert-file`/`-tls-key-file`) or use the SSH tunnel.

`llm doctor` reports the combination that matters: port 8080 answering on a
non-loopback address with no key in force.

## Opening it up on purpose

Everything binds to loopback until you say otherwise:

```bash
sudo env LLM_BIND=0.0.0.0 bash setup-system.sh
```

That renders the systemd units with the wider bind address and adds firewall
rules **for your own subnet only** (detected, or set `LLM_LAN=<your-subnet>`).
`LLM_LAN=none` skips the firewall step entirely.

Prefer not to open ports at all? Forward them over SSH instead — the server stays
loopback-only and only you reach it:

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 <user>@<server-ip>
```

For anything beyond a trusted LAN, put a reverse proxy with real authentication
(and TLS) in front, and leave the services on loopback behind it.

## The registry token

Writes to the registry (`PATCH`, `POST /api/models`, the MCP tools that change
configuration) require the header `X-LLM-Token`. The token is generated on first
start with `secrets.token_urlsafe(24)`, stored in `config/api-token` with mode
`0600`, and is gitignored. Print it with:

```bash
llm api token
```

**Reads are unauthenticated by default.** The catalog exposes model names, file
paths, context sizes, VRAM figures and Hugging Face provenance — nothing secret,
but it does describe your machine. If that matters, keep port 8081 on loopback and
reach it through SSH, or put it behind the same proxy as everything else.

## What is deliberately not in this repository

- `config/api-token` — generated per machine
- `config/llama-swap.yaml` — the live configuration, rendered by `llm init`
- `config/hardware.env`, `config/comfyui.env` — card numbers of this machine
- `openwebui-data/` — the chat database, including any keys pasted into a chat
- the venvs and downloaded models

## Reporting something

Open an issue. If you would rather not do that publicly, say so in a minimal issue
without details and we will find another channel.
