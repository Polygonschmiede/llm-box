# Installing — from a bare ROCm machine to a working endpoint

Everything here runs as your normal user except one `sudo` step. Nothing is installed
outside the repository directory, `~/whisper.cpp`, `~/comfyui` and four systemd user
units.

## Prerequisites

| | |
|---|---|
| OS | Linux. Developed and tested on Ubuntu. |
| GPU | one or more ROCm-supported AMD Radeon cards |
| ROCm | installed, with `rocm-smi` and `hipcc` on `PATH` |
| Build | `cmake`, `build-essential`, `git`, `curl` |
| Python | [`uv`](https://docs.astral.sh/uv/) — it brings its own Python 3.12 |
| Disk | ~10 GB for a llama.cpp build, plus your models |

ROCm itself is deliberately **not** installed by this project: it is several GB and
the right path differs per distribution and card generation. On Ubuntu the distro
packages are usually enough:

```bash
sudo apt-get install rocm-smi rocminfo hipcc rocm-device-libs
```

Otherwise follow AMD's instructions at <https://rocm.docs.amd.com/>.
`uv`, if missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 1. Clone

```bash
git clone https://github.com/Polygonschmiede/llm-box.git ~/llm
cd ~/llm
```

The directory name does not matter. `LLM_HOME` is derived from where `bin/llm` sits,
so the repository works anywhere; set `LLM_HOME` explicitly only if you want to
override that.

Handy: symlink the CLI onto your `PATH` so you can call `llm` from anywhere.

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/bin/llm" ~/.local/bin/llm
```

## 2. The sudo step

```bash
sudo bash setup-system.sh
```

This installs the build dependencies, checks for ROCm, adds you to the `render` and
`video` groups, enables SSH at boot, turns on lingering (so the services survive a
logout and start at boot), sets up Wake-on-LAN for the detected adapter, adds
firewall rules if `ufw` is active, and installs the four systemd user units.

Everything machine-specific — your username, the repository path, the network
adapter, the subnet — is detected, not assumed. Overridable:

```bash
sudo env LLM_NIC=eth0 LLM_LAN=<your-subnet> LLM_BIND=0.0.0.0 bash setup-system.sh
```

Your subnet, if you want to set it by hand:
`ip -o -4 route show dev "$(ip -o -4 route show to default | awk '{print $5}')" scope link`

`LLM_BIND` decides whether the services listen on `127.0.0.1` (the default) or on
every interface. Read [../SECURITY.md](../SECURITY.md) before choosing `0.0.0.0`.

**Log out and back in now.** The `render`/`video` group membership only takes effect
in a new session, and without it nothing can talk to the GPU.

## 3. Configuration

```bash
llm init
```

This renders `config/llama-swap.yaml` from `config/llama-swap.example.yaml`, filling
in the real paths. It refuses to overwrite an existing configuration.

The template also carries a set of proven model entries, all commented out. They are
settings that were measured to work well on 32 GB Radeon cards, including the MTP
flags — useful as a reference, but they point at model files you do not have yet, so
they stay inactive until you download the matching model.

## 4. Python environments

```bash
llm setup
```

Creates two environments with `uv`, deliberately separate:

- `venv-api/` (~33 MB) for the registry — `fastapi`, `uvicorn`, `mcp`
- `venv-webui/` (~6.4 GB) for Open WebUI

They are separate so that an Open WebUI upgrade cannot move `fastapi`/`pydantic`/`mcp`
underneath the registry and take every agent offline. Both use Python 3.12; Open WebUI
requires `>=3.11,<3.13`.

## 5. The engines

```bash
llm update swap          # download the llama-swap binary
llm update llama         # clone llama.cpp, build it, smoke-test it, activate it
```

`llm update llama` is also the setup step: if llama.cpp is not there it is cloned.
The build takes a few minutes. The gfx target and HIP compiler are detected from
`rocm-smi` and `hipconfig`.

Optional, for speech to text:

```bash
llm update whisper       # clone and build whisper.cpp the same way
```

## 6. Check

```bash
llm doctor
```

This walks the whole chain — tools, group membership, detected cards, whether
llama.cpp agrees about the card count, configuration, engines, environments,
services — and for each failure prints the command that fixes it. Get this green
before going further.

## 7. First model

```bash
llm add unsloth/Qwen3-8B-GGUF Q4_K_M
llm restart
llm url
```

`llm add` downloads the GGUF, reads its geometry out of the header, works out the KV
cache size, checks that it fits on the target card, and writes the entry. Then:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b-q4_k_m","messages":[{"role":"user","content":"Hello"}]}'
```

Open the chat interface on port 3000 and pick the model from the dropdown. With
`WEBUI_AUTH=True` (the shipped default) Open WebUI asks you to create an account on
first visit; the first account is the administrator.

## Moving the installation later

The live configuration contains absolute paths, because llama-swap does not expand
environment variables inside its command lines. After moving the directory:

```bash
sed -i "s|/old/path|/new/path|g" config/llama-swap.yaml
sudo bash setup-system.sh      # re-render the units
llm doctor
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `llm doctor` says no card detected | not in `render`/`video` yet (log out and back in), or ROCm not installed |
| a service will not start | `llm llama logs` / `llm api logs` — the unit's own log says why |
| a model is missing from `/v1/models` | llama-swap was not restarted: `llm restart` |
| nothing reachable from another machine | services on loopback (`LLM_BIND=0.0.0.0`) or the firewall — see [REMOTE.md](REMOTE.md) |
| model fails to load with out of memory | it does not fit per card; `llm gpu list` shows what is occupying it |
| download crawls at ~50 KB/s | the Xet backend; `llm add` sets `HF_HUB_DISABLE_XET=1`, restart the command |
| card numbers look wrong after a hardware change | `llm gpu sync` |
| `torch.cuda.is_available()` false in ComfyUI | see [COMFYUI.md](COMFYUI.md) |

More: [MODELS.md](MODELS.md), [FLAGS.md](FLAGS.md), [UPDATES.md](UPDATES.md).
