# Staying current — with a way back

Short version: **`llm update`** shows what is new, **`llm update all`** updates
everything installed here, and **`llm rollback <what>`** undoes it. The same three
things are on the **System tab** of the control page, for anyone who would rather
click than open a shell.

## Why this is not just "apt upgrade"

There are no ready-made Linux binaries of llama.cpp for recent Radeon cards — the
official ROCm releases are Windows-only. So llama.cpp is **built here**. llama-swap
by contrast is a prebuilt Go binary and is simply swapped. Open WebUI and ComfyUI
are Python, handled by `uv` and git.

Being the one thing here that is not built from source, that binary gets checked:
its SHA-256 is compared against the checksum list published in the same GitHub
release **before** the archive is unpacked, and a release without such a list is
refused rather than installed on trust. What that proves is that the tarball
matches what the release says it contains — release artefacts are not signed, so
it does not prove who built it.

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

The control page reaches the same three through the registry, as jobs whose log you
can follow: `POST /api/updates/check`, `POST /api/updates/<component>` and
`POST /api/rollback/<component>` — see [API.md](API.md). Only one runs at a time;
they share the repositories and the services.

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
   has **changes to tracked files**, or the requested tag does not exist.

On that third point: untracked files do **not** count. They cannot, because `build`
is a symlink this tool creates itself, and an upstream `.gitignore` that writes
`build/` with a trailing slash ignores the *directory* and not the *symlink*. Counting
untracked files meant whisper.cpp reported "local changes" about its own build link
and refused every update forever. If an untracked file really is in the way of the
checkout, git says so, with the path.

During an update llama-swap is **briefly stopped** (a few seconds), because VRAM and
the binary path change. In-flight requests are cut off.

`llm update swap <version>` used to install the *newest* release whatever version
you asked for, and then report itself as the version you named — it read
`releases/latest` regardless of the argument. It asks for the release you named.

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

The flags live as `LCPP_CMAKE_FLAGS` and `hip_flags()` in `lib/update.sh`.

**Tip:** with `ccache` installed, rebuilds of the same version get much faster.
Nothing here passes `GGML_CCACHE`; llama.cpp's own `ggml/CMakeLists.txt` defaults
it to `ON`, and it does nothing until `ccache` is on `PATH`.

## Versions: release tags, not master

Updates go to **release tags** (`bXXXXX`), not to the master HEAD. That is
reproducible and lets you name a version. llama.cpp publishes several times a day —
you do not have to follow every tag. Update when you need a new model or feature.

### Two names for one commit

whisper.cpp publishes rolling bot releases (`bNNNN`) *and* hand-cut releases
(`v1.x.y`), and GitHub's "latest release" is whichever was published last and not
flagged as a prerelease. So `active v1.9.2` against `latest b4938` can mean a real
update — or the very same commit under the other name.

Comparing the two *names* therefore reports an update that does not exist, forever.
Instead `.update-cache` stores the commit each latest tag points at, as
`<tag> <sha>`, and "up to date" means **the same commit**. When either side cannot be
resolved, the name comparison is used and an update is offered — offering one too
often is the harmless direction. `llm update` and the control page share this, so
they cannot contradict each other.

## When something does go wrong

```bash
llm rollback llama              # back to the previous version
llm versions                    # what is actually installed
llm doctor                      # is the chain still intact
llm logs                        # what llama-swap says
cat .build-b10408.log           # the full build log
cat .smoke.log                  # output of the smoke-test server
```
