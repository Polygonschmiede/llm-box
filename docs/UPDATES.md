# Staying current — with a way back

Short version: **`llm update`** shows what is new, **`llm update all`** updates
everything installed here, and **`llm rollback <what>`** undoes it.

## Why this is not just "apt upgrade"

There are no ready-made Linux binaries of llama.cpp for recent Radeon cards — the
official ROCm releases are Windows-only. So llama.cpp is **built here**. llama-swap
by contrast is a prebuilt Go binary and is simply swapped. Open WebUI and ComfyUI
are Python, handled by `uv` and git.

## How the rollback works

Each llama.cpp version gets its **own build directory**, and `build` is only a
symlink:

```
llama.cpp/
├── build -> build-b10408      ← active (every config path points through here)
├── build-b10408/              new version
└── build-571d0d540/           fallback
```

Switching and rolling back is therefore a **symlink change** — seconds, no rebuild.
The active build plus one fallback are kept (`KEEP_BUILDS` in `bin/llm`); older ones
are deleted. A llama.cpp build is ~1.1 GB, a whisper.cpp build ~174 MB.

Open WebUI cannot work that way — a Python environment per version would cost 6.4 GB
each. Instead, before an upgrade it saves two small things:

- the complete dependency closure (`uv pip freeze`)
- a copy of `webui.db`

The database is the important half: Open WebUI runs **forward-only** alembic
migrations at startup, so downgrading the code against an already-migrated database
is not safe without a snapshot. At about 1 MB per copy, keeping it is free.

ComfyUI is a git checkout, so its rollback is a checkout of the recorded ref plus a
dependency pass.

## Commands

```bash
llm update                  # just look: active vs. latest
llm update llama            # llama.cpp: fetch → build → smoke test → switch
llm update swap             # replace the llama-swap binary (old one is kept)
llm update whisper          # whisper.cpp: same procedure
llm update ui               # Open WebUI (freeze + database snapshot first)
llm update comfy            # ComfyUI (tag checkout plus dependencies)
llm update all              # everything that is installed here
llm update llama b10408     # build/activate one SPECIFIC version

llm versions                # installed builds and versions (● = active)
llm rollback llama          # back one version
llm rollback llama 571d0d540    # to that specific one
llm rollback ui             # Open WebUI, including its database
llm rollback comfy          # ComfyUI back to the recorded ref
```

`llm status` shows the active versions at the bottom and points out updates. The
GitHub and PyPI queries are refreshed **once a day** in the background
(`.update-cache`), so `llm status` never waits on the network.

## What protects you from a broken update

1. **The old build stays complete** — an update adds, it never overwrites.
2. **A smoke test before switching.** The new llama.cpp build has to load a real
   model from your configuration (the smallest one), reach `/health` **and** return
   a short answer. Only then is the symlink moved. If it fails, the old version stays
   active and the build directory is left there to look at. For whisper.cpp the test
   transcribes the bundled `samples/jfk.wav`; for Open WebUI it polls
   `:3000/health`; for ComfyUI it checks that torch still sees the GPU.
3. **Self-healing on switch.** If the API does not answer after the restart, `llm`
   puts the symlink back and starts again. If an Open WebUI upgrade fails, the freeze
   snapshot is reinstalled automatically.
4. **Refusal before building** when: less than 8 GB of disk is free, the source repo
   has local changes, or the requested tag does not exist.

During an update llama-swap is **briefly stopped** (a few seconds), because VRAM and
the binary path change. In-flight requests are cut off.

Two things a rollback cannot fix, so they are worth knowing:

- Custom ComfyUI nodes may expect a newer ComfyUI API and break on a downgrade.
  Nothing under `custom_nodes/` is touched either way.
- An Open WebUI downgrade restores the database snapshot, which means anything
  written *after* the upgrade is not in it.

## Build time and flags

A llama.cpp build takes a couple of minutes on a modern desktop CPU. The two flags
that depend on your hardware are detected rather than hardcoded — see
[FLAGS.md](FLAGS.md), "Build flags". Two further flags are worth knowing:

| Flag | Why |
|------|-----|
| `-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON` | every build loads its **own** `.so` files. Without it the RUNPATH is absolute to `…/build/bin`, and a test build would load the libraries of the *active* build — which defeats the whole point of testing before switching. |
| `-DLLAMA_BUILD_TESTS=OFF` | saves build time; the server does not need the test binaries |

The flags live as `LCPP_CMAKE_FLAGS` and `hip_flags()` in `bin/llm`.

**Tip:** with `ccache` installed, rebuilds of the same version get much faster
(`GGML_CCACHE=ON` is already set but does nothing without it).

## Versions: release tags, not master

Updates go to **release tags** (`bXXXXX`), not to the master HEAD. That is
reproducible and lets you name a version. llama.cpp publishes several times a day —
you do not have to follow every tag. Update when you need a new model or feature.

## When something does go wrong

```bash
llm rollback llama              # back to the previous version
llm versions                    # what is actually installed
llm doctor                      # is the chain still intact
llm logs                        # what llama-swap says
cat .build-b10408.log           # the full build log
cat .smoke.log                  # output of the smoke-test server
```
