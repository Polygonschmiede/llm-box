# llm-box

A self-hosted LLM server for Linux machines with AMD Radeon cards. One
OpenAI-compatible endpoint, models that load and unload themselves, and one `llm`
command to drive the whole thing.

It is a thin, readable layer over [llama.cpp](https://github.com/ggml-org/llama.cpp)
and [llama-swap](https://github.com/mostlygeek/llama-swap) — not a rewrite of them,
and not Ollama. No Docker. No cloud. ROCm first.

```
$ llm status
Services:
  ● LLM API   Port 8080  active   (HTTP 302)
  ● Registry  Port 8081  active   (HTTP 200)
  ● Chat UI   Port 3000  active   (HTTP 200)
  ○ Images    Port 8188  inactive (HTTP 000)

Loaded model: qwen3.8-27b-q6_k
Cards:
  card 0  junction 30°C  VRAM 28.6/32 GB  AMD Radeon AI PRO R9700  [qwen3.8-27b-q6_k]
  card 1  junction 27°C  VRAM 0.1/32 GB   AMD Radeon AI PRO R9700
Versions: llama.cpp b10453 · llama-swap v250 · whisper.cpp v1.9.2  up to date
```

## What you get

- **One endpoint for everything.** `http://<server>:8080/v1` serves chat,
  embeddings, reranking and speech-to-text. The `model` name in the request decides
  which; llama-swap loads it on demand and unloads it after 15 idle minutes.
- **`llm` instead of llama.cpp command lines.** `llm add unsloth/Qwen3-8B-GGUF Q4_K_M`
  downloads a model, works out its context and VRAM needs from the GGUF header,
  checks whether it actually fits, and writes the configuration.
- **Versioned engine builds.** Each llama.cpp version gets its own build directory
  with a symlink pointing at the active one, so switching or rolling back is a
  symlink change, not a rebuild — and a smoke test runs before anything switches.
- **A registry for agents** on port 8081: an HTTP catalog and an MCP server that
  answer what models exist, how they are configured, which card they sit on and
  where they came from — and that accept changes.
- **1, 2 or more cards.** Card count, gfx target and HIP compiler are detected, not
  hardcoded. Pin a small model to one card and run two models at once.
- **Several clients and their subagents at the same time.** Models serve four
  requests in parallel instead of queueing, and a **role** — one name in front of
  several models — can send the overflow to the second card without the client
  knowing. See [docs/FLAGS.md](docs/FLAGS.md).

## Requirements

- Linux with a working **ROCm** installation (`rocm-smi` and `hipcc` on `PATH`).
  Developed and tested on Ubuntu with 2× AMD Radeon AI PRO R9700 (gfx1201, 32 GB
  each) and ROCm 7.1. Any ROCm-supported Radeon should work.
- The user in the `render` and `video` groups
- [`uv`](https://docs.astral.sh/uv/) for model downloads and the Python
  environments; `cmake` and a compiler for building llama.cpp
- ~10 GB of disk for an engine build, plus whatever your models need

`llm doctor` checks all of this and names the command that fixes each failure.

## Quickstart

```bash
git clone https://github.com/Polygonschmiede/llm-box.git ~/llm
cd ~/llm

sudo bash setup-system.sh     # packages, GPU groups, services, firewall
./bin/llm init                # render the configuration from the template
./bin/llm setup               # create the Python environments
./bin/llm update llama        # fetch and build llama.cpp, then smoke-test it
./bin/llm update swap         # fetch the llama-swap binary
./bin/llm doctor              # verify the whole chain
```

Log out and back in once after `setup-system.sh` (the GPU groups only take effect
on a new session). Details and troubleshooting: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Your first model

```bash
llm add unsloth/Qwen3-8B-GGUF Q4_K_M     # you choose; nothing downloads on its own
llm restart
llm url                                  # the addresses to point clients at

curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b-q4_k_m","messages":[{"role":"user","content":"Hello"}]}'
```

## Everyday commands

`llm help` lists all of them. The ones you will actually use:

```bash
llm status                     # services, loaded model, GPU temperature and VRAM
llm ls  /  llm rm <name>       # list / remove models
llm add … --gpu 1              # pin a model to one card
llm add … --slots 4            # serve four requests at once instead of queueing
llm role                       # roles: one name, several models behind it
llm gpu list  /  llm gpu sync  # cards; re-match the config after a hardware change
llm speed                      # what token prediction is and when it helps

llm llama|api|ui|comfy <on|off|restart|status|logs>
llm on  /  llm off             # everything on / everything off (frees the GPUs)

llm update                     # is anything newer available?
llm update all                 # update, with a smoke test before each switch
llm rollback llama             # back to the previous version in seconds

llm meta                       # which Hugging Face repo did each model come from
llm api client                 # ready-made setup line for a client machine
```

## How it fits together

```
Browser  ──►  Open WebUI :3000  ─┐
Code     ──►  API        :8080  ─┼─►  llama-swap  ──►  loads and swaps the
Images   ──►  ComfyUI    :8188  ─┘                     right model onto the cards
Agent    ──►  Registry   :8081  ────►  catalog + configuration + MCP
```

llama-swap is the switchboard: clients talk to **one** endpoint, it starts the
requested model and frees the VRAM again when the model goes unused. Open WebUI
loads nothing itself — it is just another client of port 8080.

## ⚠ Security

The shipped defaults bind every service to `127.0.0.1`. That is deliberate:
**llama-swap and ComfyUI have no authentication at all**, and anyone who can reach
port 8080 can run any configured model. Registry reads are unauthenticated too;
only writes need a token.

Open it up on purpose with `sudo env LLM_BIND=0.0.0.0 bash setup-system.sh`, which also
adds firewall rules for your own subnet — or forward the ports over SSH and leave
the services on loopback. Read **[SECURITY.md](SECURITY.md)** before you expose
anything.

## Using it from other tools

Anything that speaks the OpenAI API works: point it at `http://<server>:8080/v1`,
use any non-empty API key (`sk-local` in the examples — llama.cpp does not check it),
and use a model name from `llm ls`. For agents there is the registry and its MCP
server, so they see the real state instead of a stale hardcoded model list — see
[docs/API.md](docs/API.md) and [docs/PI.md](docs/PI.md).

Better than a model name: a **role** from `llm role`. A client asks for `coder` or
`chat` and the server decides which model answers — including sending a third and
fourth concurrent request to the second card. Point your main agent at one role and
its subagents at another, and you never touch the client again when models change.

## Documentation

| | |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | from a bare ROCm machine to a working endpoint |
| [docs/MODELS.md](docs/MODELS.md) | choosing, adding and removing models; quants; per-model settings |
| [docs/FLAGS.md](docs/FLAGS.md) | every llama.cpp option this stack sets, and why |
| [docs/UPDATES.md](docs/UPDATES.md) | staying current, with rollback |
| [docs/API.md](docs/API.md) | the registry: HTTP catalog and MCP |
| [docs/PI.md](docs/PI.md) | connecting the pi agent |
| [docs/COMFYUI.md](docs/COMFYUI.md) | image generation alongside the LLMs |
| [docs/REMOTE.md](docs/REMOTE.md) | SSH, port forwarding, Wake-on-LAN |
| [SECURITY.md](SECURITY.md) | what listens where, and how to lock it down |

## Repository layout

```
bin/llm                 the CLI (everything goes through here)
bin/llm-api.py          registry: HTTP catalog + MCP server
lib/llmreg.py           the library both use: GGUF headers, VRAM estimates,
                        card detection, llama-swap config read/write
lib/update.sh           updating and rolling back the engines (sourced by bin/llm)
config/                 configuration template and requirements
systemd/                user service templates
pi/extensions/          pi agent integration
tests/                  four suites, no GPU needed — see CONTRIBUTING.md
docs/                   the documentation above
```

## Contributing

`bash tests/run-all.sh` — no GPU needed, and it does not touch your
configuration. Details, the lint commands and where code belongs:
[CONTRIBUTING.md](CONTRIBUTING.md). What changed when:
[CHANGELOG.md](CHANGELOG.md).

## Credits

Built on [llama.cpp](https://github.com/ggml-org/llama.cpp),
[llama-swap](https://github.com/mostlygeek/llama-swap),
[whisper.cpp](https://github.com/ggml-org/whisper.cpp),
[Open WebUI](https://github.com/open-webui/open-webui) and
[ComfyUI](https://github.com/comfyanonymous/ComfyUI). All the hard parts are theirs.

MIT licensed — see [LICENSE](LICENSE).
