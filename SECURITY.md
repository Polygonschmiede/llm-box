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

**The key is also written into `config/llama-swap.yaml` in clear text**, because
llama-swap needs it there and rejects an empty `${env.VAR}` expansion. That file
used to be mode 644, which quietly undid the mode 600 on `config/api-key` beside
it — any account on the machine could read the key out of the large file. Every
write now narrows it to 600, and `llm doctor` says so if an older installation is
still world-readable. It costs nothing: llama-swap runs as the same user.

`llm doctor` reports the two combinations that matter: port 8080 answering on a
non-loopback address with no key in force, and a configuration that holds a key
while being readable by everyone.

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

`LLM_API_REQUIRE_AUTH=1` closes reads instead. Until now it closed them only over
HTTP: the MCP tools `list_models`, `get_model`, `gpu_status` and `job_status`
answered without a token whatever that variable said — the same catalog and the
same filesystem paths, through a different door. They follow the switch now.

Token comparison is `secrets.compare_digest` rather than `==`, so a wrong token
takes the same time to reject as a nearly-right one.

## What gets downloaded, and what is checked

Everything this project installs it builds from source over HTTPS — llama.cpp,
whisper.cpp and ComfyUI are git clones, and a requested tag is resolved to its
commit and compared, so a moved tag does not silently become a different build.

The **one** exception is the `llama-swap` binary, which comes from a GitHub
release as a tarball. Its only gate used to be that the extracted file answered
`--version`, which a substituted tarball would also do. It is now verified with
`sha256sum` against the checksum list published in the same release, before the
archive is unpacked, and a release that ships no checksum list is refused rather
than installed unverified.

Not covered, and worth knowing: release artefacts are not signed, so this proves
the tarball matches what that release says it contains — not who built it.
Python packages come from PyPI unpinned (`fastapi`, `uvicorn`, `open-webui`) and
torch from `download.pytorch.org`; `pip-audit` runs against the registry's
requirements in CI, but the chat UI's ~500 transitive packages are not this
project's to vouch for.

Model files are checked differently: `llm add` verifies that the revision exists
at the recorded commit through the Hugging Face API, and records the SHA-256 that
the download reported. It does **not** recompute the digest from the file on
disk, so that number is provenance, not an integrity check.

## What is deliberately not in this repository

- `config/api-token` — generated per machine
- `config/llama-swap.yaml` — the live configuration, rendered by `llm init`
- `config/hardware.env`, `config/comfyui.env` — card numbers of this machine
- `openwebui-data/` — the chat database, including any keys pasted into a chat
- the venvs and downloaded models

## Reporting something

Privately, through
[GitHub Security Advisories](https://github.com/Polygonschmiede/llm-box/security/advisories/new)
— that channel is open on this repository and does not make the report public.

Anything that is only a limitation of the design above (llama-swap being
default-allow, reads on 8081 being open, ComfyUI having no authentication) is
documented on purpose and belongs in a normal issue. Anything that gets past a
door this file claims is shut is worth the private channel.
