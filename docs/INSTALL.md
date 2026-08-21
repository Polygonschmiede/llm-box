# Installing — from a bare machine to a working endpoint

Everything here runs as your normal user except one `sudo` step. Nothing is installed
outside the repository directory, `~/whisper.cpp`, `~/comfyui` and four systemd user
units.

## Two ways to reach the GPU

Pick one before you start; it is recorded in `config/hardware.env` and everything
after that follows it.

| | **ROCm** | **Vulkan** |
|---|---|---|
| Cards | AMD, and only those ROCm supports | anything with a Vulkan driver: AMD (including cards ROCm dropped), Intel, NVIDIA |
| To install | several GB, a different path per distribution and card generation | three distribution packages |
| Built for | the exact ISA of the cards it can see (`AMDGPU_TARGETS`) | SPIR-V, compiled once, runs on every device |
| Speed | measure it; see below | measure it; see below |
| Card readings | temperature, watts, utilisation from `rocm-smi` | the same, from amdgpu's sysfs — and nothing at all on a non-AMD driver |
| ComfyUI | works | **no** — PyTorch has no Vulkan build |

**Do not assume ROCm is faster.** Measured on the development machine — one
R9700, Qwen3.5-4B-Q4_K_M, 200-token generations, three runs each:

| | tokens/second |
|---|---|
| Vulkan (RADV, mesa 26.0) | 111.9 · 114.2 · 113.8 |
| ROCm 7.1 | 97.7 · 99.4 · 99.5 |

So on that hardware and that model Vulkan was about 14 % **faster**. That is one
small dense model on one card, generation only — it says nothing about large
models, several cards at once, or prompt processing, none of which were measured.
Take it as a reason to test your own case rather than as a ranking.

ROCm is still the default where it is complete, for reasons other than raw speed:
it is the better-supported path, `llm speed`'s draft-model features are exercised
there, and ComfyUI needs it. Vulkan exists so that the answer to "my card is not
on AMD's list" is not "this project is not for you".

## Prerequisites

| | |
|---|---|
| OS | Linux. Developed and tested on Ubuntu. |
| GPU | one or more cards, see the table above |
| Build | `cmake`, `build-essential`, `git`, `curl` |
| Python | [`uv`](https://docs.astral.sh/uv/) — it brings its own Python 3.12 |
| Disk | ~10 GB for a llama.cpp build, plus your models |

**For ROCm**, it is deliberately **not** installed by this project: it is several
GB and the right path differs per distribution and card generation. On Ubuntu the
distro packages are usually enough:

```bash
sudo apt-get install rocm-smi rocminfo hipcc rocm-device-libs
```

Otherwise follow AMD's instructions at <https://rocm.docs.amd.com/>.

**For Vulkan**, `setup-system.sh` installs the build dependencies itself, but a
**driver** is yours to provide — `mesa-vulkan-drivers` on AMD and Intel, the
proprietary one on NVIDIA. `vulkaninfo --summary` has to list your card before
anything here can use it. The three build packages, if you want them by hand:

```bash
sudo apt-get install glslc libvulkan-dev spirv-headers vulkan-tools
```

All three are needed: `glslc` compiles the shaders, `libvulkan-dev` carries the
headers and the link-time library, and `spirv-headers` the cmake config ggml
looks for. Leaving any one out fails at configure — which is why `llm update
llama` checks for all three before it touches anything.

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

This installs the build dependencies, checks whichever backend applies — and for
Vulkan installs its three packages — adds you to the `render` and `video` groups, enables SSH at boot, turns on lingering (so the services survive a
logout and start at boot), sets up Wake-on-LAN for the detected adapter, adds
firewall rules if `ufw` is active, and installs the four systemd user units.

Everything machine-specific — your username, the repository path, the network
adapter, the subnet — is detected, not assumed. Overridable:

```bash
sudo env LLM_NIC=eth0 LLM_LAN=<your-subnet> LLM_BIND=0.0.0.0 bash setup-system.sh
```

`LLM_BACKEND=rocm` or `=vulkan` decides which backend's dependencies this step
looks after. Left unset it takes whichever is already installed, ROCm first.

Your subnet, if you want to set it by hand:
`ip -o -4 route show dev "$(ip -o -4 route show to default | awk '{print $5}')" scope link`

`LLM_BIND` decides whether the services listen on `127.0.0.1` (the default) or on
every interface. Read [../SECURITY.md](../SECURITY.md) before choosing `0.0.0.0`.

**Log out and back in now.** The `render`/`video` group membership only takes effect
in a new session, and without it nothing can talk to the GPU.

## 3. Configuration

```bash
llm init                     # backend detected
llm init --backend vulkan    # or state it
```

This renders `config/llama-swap.yaml` from `config/llama-swap.example.yaml`, filling
in the real paths, and records the backend in `config/hardware.env` along with the
card numbers. It refuses to overwrite an existing configuration.

Without `--backend` it takes ROCm when `rocm-smi` and `hipcc` are both there and
report a card, Vulkan when `vulkaninfo` reports one, and says so. To change it
later: `llm gpu backend vulkan`, which rewrites the configuration and relinks the
engines if a build for that backend already exists. The first build of each
backend costs a build; after that, switching is a symlink change.

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
The build takes a few minutes.

Under ROCm the gfx target and the HIP compiler are detected from `rocm-smi` and
`hipconfig`. Under Vulkan there is nothing to detect — one cmake flag, no ISA, no
special compiler — which is also why a Vulkan build cannot be wrong about
hardware it has not met.

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
| `llm doctor` says no card detected | not in `render`/`video` yet (log out and back in), or the backend's tools are missing — `llm doctor` names which |
| `llm doctor` says llama.cpp reports no device | the build is for the other backend: `llm update llama` rebuilds |
| `llm comfy` refuses | ComfyUI needs ROCm; see [COMFYUI.md](COMFYUI.md) |
| a service will not start | `llm llama logs` / `llm api logs` — the unit's own log says why |
| a model is missing from `/v1/models` | llama-swap was not restarted: `llm restart` |
| nothing reachable from another machine | services on loopback (`LLM_BIND=0.0.0.0`) or the firewall — see [REMOTE.md](REMOTE.md) |
| model fails to load with out of memory | it does not fit per card; `llm gpu list` shows what is occupying it |
| download crawls at ~50 KB/s | the Xet backend; `llm add` sets `HF_HUB_DISABLE_XET=1`, restart the command |
| card numbers look wrong after a hardware change | `llm gpu sync` |
| `torch.cuda.is_available()` false in ComfyUI | see [COMFYUI.md](COMFYUI.md) |

More: [MODELS.md](MODELS.md), [FLAGS.md](FLAGS.md), [UPDATES.md](UPDATES.md).
