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
