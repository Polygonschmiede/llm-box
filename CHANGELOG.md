# Changelog

All notable changes to llm-box. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version numbers describe **llm-box itself**, not the engines it drives —
`llm versions` reports those, and `llm update` moves them independently.

## [Unreleased]

### Added

- **A settings page** at `:8081/ui`, served by the registry. One file, no build
  step, no external requests, same origin as the API. Models with their VRAM
  budget *per card* rather than as a sum, provenance, slots and thinking depth
  explained rather than just numbered; roles with their effective context in
  front, because a role reports the smallest of its targets; the card list with
  the `gpu sync` diff and the group flags; versions with rollback candidates.
  Every change previews through `?dryRun=true` first. Charts, logs and a
  playground are deliberately absent — llama-swap's own UI does those better.
- **Roles are writable over HTTP**: `PUT`/`DELETE /api/roles/{name}`. Until now
  `llm role` on the server was the only way in, so a UI or a remote agent could
  see a role and not change it.
- `GET /api/versions`, `GET /api/config` (macros and groups, which llama-swap
  cannot show at all) and `GET /api/config/diff`.
- **Session cookies** so a page need not keep the token in a form field:
  `POST /api/session` exchanges it for an `HttpOnly` cookie, `GET /api/session`
  reports `canWrite`, `DELETE` ends it. Reads stay open by default — the pi
  extension reads without a token — and `LLM_API_REQUIRE_AUTH=1` requires it for
  reads too, which is worth setting if 8081 is reachable beyond your own machine.
- `tests/ui-matrix.sh` runs the page's script under a minimal DOM against
  payloads from a throwaway `LLM_HOME`. Curling `/ui` would not do: it answers
  200 whatever the JavaScript does, and the first version of that check passed
  while every element on the page read `[object Object]`.

- The registry reports **which** `reasoning_effort` values a model's chat
  template accepts, read out of the GGUF header, plus the default and whether
  thinking is preserved across turns: `runtime.reasoningEffort` and
  `compat.reasoningEfforts` in what pi receives. llama.cpp only reports
  `supports_reasoning_effort: true`, so a client offering the usual OpenAI
  low/medium/high picker gets an HTTP 500 from Jinja on two of the three —
  Qwen3.8 accepts `xhigh`, `medium` and `low` and raises on `high`.
- `docs/FLAGS.md` gains a "Thinking depth" section: the accepted values come
  from the model rather than from llama.cpp, `--reasoning-effort` is a floor and
  not a ceiling, old `reasoning_content` accumulates unless
  `--no-reasoning-preserve` says otherwise, and switching effort mid-session
  invalidates the whole prompt cache because the instruction is rendered ahead
  of the system prompt.

### Fixed

- `docs/FLAGS.md` listed `high` as a valid `reasoning_effort`. On the model this
  project ships examples for, it is an HTTP 500.
- **A service could stay dead after its configuration was fixed.** systemd counts
  manual starts towards its rate limit, and the default is five per ten seconds —
  so `llm off && llm on` plus a restart was enough to trip it, after which every
  start was refused with "start request repeated too quickly" until someone ran
  `systemctl --user reset-failed`. The units now disable the limiter, so a
  rejected configuration crash-loops visibly and recovers by itself the moment
  the file is fixed. Measured: broken config → `activating`, file repaired →
  `active` within ten seconds with no command in between.
- `llm restart` and `llm on` reported success while the service was down —
  `systemctl restart` returns 0 for a unit that starts and then exits. Both now
  clear the failed state, verify the unit is really running, and print the
  service's own error when it is not. `llm on` reports per unit instead of one
  "on." with stderr discarded.

### Changed

- `qwen3.8-27b-q6_k` and `qwen3.8-27b-q8_0` carry
  `--reasoning-effort low --no-reasoning-preserve`. The template defaults to
  `xhigh`, and the harnesses that drive local models mostly send no effort field
  at all, so the default was the slowest setting with no way to notice.

## [1.2.0] — 2026-08-20

Maintenance: the first tests for the parts that decide whether a model loads,
CI, and five bugs those tests found.

### Fixed

- `HIP_VISIBLE_DEVICES` was written with the **logical** card number while that
  variable counts absolute cards the way `rocm-smi` does. `gpu_of()` already
  read it back through `to_logical()`, so read and write disagreed. Invisible
  on a machine whose discrete cards sit at 0,1; on an iGPU-first machine
  `PATCH {"gpu": 0}` on a whisper model addressed the wrong card.
- `sync_groups()` returned the configuration unchanged when its marker block
  was missing, and all four callers reported success — while every card-pinned
  model quietly stayed in llama-swap's default group, which swaps and is
  exclusive. It now creates the block, or refuses if there is no `models:`
  section at all.
- `derive()` looped forever on a model whose `-m` path lies outside `models/`.
  A single such entry hung `llm ls` and `GET /api/models`.
- `set_flag(name, None)` removed the bare switch **and the token after it**, so
  patching slots twice turned `-np 3 -kvu` into `3 -kvu` — a command line
  llama-server refuses to start. Reachable through `PATCH {"parallel": N}`.
- The VRAM fit check ran against the state *before* the patch, so adding
  `-ctk q8_0` — which halves the KV cache — was judged at f16 and could be
  refused although it frees room.
- `PATCH /api/models/{id}` accepted a card the machine does not have; the check
  existed only on `POST /api/models`.
- A checkout without a configuration answered `GET /api/models` with a 500 and
  a traceback. It now returns 503 naming `llm init`.
- `llm llama|llm|api <action>` could never match `api`, and would have been
  wrong if it had: `api` is the registry. Found by shellcheck.

### Added

- `tests/run-all.sh` plus three new suites — 158 checks in total, none of which
  needs a GPU or touches the machine's own configuration. `tests/lib.sh` holds
  the shared harness; a temporary `LLM_HOME` exercises the real config write
  path.
- `.github/workflows/ci.yml`: ruff, shellcheck, the suites, a guard against
  committing machine paths or LAN addresses, and a markdown link check.
- `pyproject.toml` (lint rules only), `.shellcheckrc`, `.editorconfig`.
- `llm --version`, and `llmBox` in `GET /api/health`. A single `VERSION` file
  feeds the CLI, the registry and `package.json`.
- This changelog, and `CONTRIBUTING.md` — which is the first place that says
  how to run the tests.
- `docs/UI.md`: the four interfaces and which one is authoritative for what.
  llama-swap ships its own ten-page web interface at `:8080/ui` and nothing here
  ever mentioned it — including that its hardware page ignores
  `HIP_VISIBLE_DEVICES` and lists an integrated GPU as a compute card, and that
  it has no configuration view at all.
- `SECURITY.md` now records that `GET /unload` on port 8080 unloads every model
  without a token — a mutating GET, so a browser prefetch is enough.
- `docs/API.md` and `docs/PI.md` state why the registry exposes nine MCP tools
  and the pi extension registers six: downloading and deleting live in pi's
  interactive `/llm` command behind a confirmation, not as agent-callable tools.

### Changed

- `kv_cache_bytes()` takes an optional pre-read GGUF header, so the three cache
  layouts can be tested without committing gigabytes of model files.
- Card-pinned models share **one** routing group (`pinned`) instead of one per
  card. The settings were identical anyway, and a `spillover` role requires all
  of its targets in a single group.
- `bin/llm` is 1096 lines, down from 1591: the update and rollback machinery for
  the five engines moved to `lib/update.sh`, which it does not share anything
  with beyond the output helpers.
- Removing a model now also drops it from every role that pointed at it, and
  deletes a role left with no targets. llama-swap validates selector targets at
  startup, so the old behaviour produced a configuration that failed on the next
  restart.
- The whole tree is English, including the shipped template's own header — which
  used to name the two path placeholders in a comment, so `llm init` substituted
  them there too and every generated configuration carried a mangled path.

## [1.1.0] — 2026-08-20

### Added

- **Slots.** Models serve four requests at once instead of queueing. `-c` is the
  total KV cache either way, so slots cost no cache — measured, the worst-case
  latency for two clients fell from 24.7 s to 5.0 s while total throughput
  stayed flat. One card has a fixed budget; slots make it fair, not faster.
- **Roles** (`llm role`): one name in front of several models, resolved per
  request. `spillover` sends the overflow to the second card without the client
  knowing. A role reports the smallest context and the intersection of the
  capabilities of its targets.
- `-cram 16384` for the prompt cache — host RAM, no VRAM. It is what makes a
  returning agent skip re-reading its whole prompt.
- `llm add --slots N`, `parallel` in `PATCH /api/models/{id}`, and `kind`,
  `kvUnified` and role entries in the catalog.

### Fixed

- Whisper pins its card through `env:` rather than `--device`, so the group
  generator never saw it and a single transcription unloaded **both** cards.
  Card groups are now written `persistent: true`.
- The catalog reported one slot for models with no `-np` flag, where llama.cpp
  actually runs four with a unified KV cache.
- `llm gpu sync` could never work: bash took the configuration lock and then
  called Python, which takes the same lock — a self-deadlock — and the success
  message was printed regardless of the exit status.
- `llm ls` printed card 0 as `-`, because 0 is falsy in Python.

### Changed

- Embedding and reranker buffers halved (`-b/-ub 4096`, `-c 4096`): the
  always-on trio dropped from 21.6 GB to 15.3 GB, freeing 6.3 GB. A single
  input over 4096 tokens is now rejected rather than truncated; batches are
  still split internally.

## [1.0.0] — 2026-08-17

Initial release. One OpenAI-compatible endpoint for chat, embeddings, reranking
and speech-to-text; models that load and unload themselves; the `llm` CLI;
versioned engine builds with rollback; a registry with an HTTP catalog and MCP
for agents; ROCm-first, no Docker.

[1.2.0]: https://github.com/Polygonschmiede/llm-box/releases/tag/v1.2.0
[1.1.0]: https://github.com/Polygonschmiede/llm-box/releases/tag/v1.1.0
[1.0.0]: https://github.com/Polygonschmiede/llm-box/releases/tag/v1.0.0
