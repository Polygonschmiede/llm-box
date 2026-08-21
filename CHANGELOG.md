# Changelog

All notable changes to llm-box. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version numbers describe **llm-box itself**, not the engines it drives —
`llm versions` reports those, and `llm update` moves them independently.

## [Unreleased]

### Added

- **`tests/mcp-matrix.sh` — the MCP surface finally has tests.** `api-matrix`
  covered the HTTP half and left the other door untested: the same catalogue and
  the same write actions, reachable over MCP, with nothing holding the token gate
  in place. Fourteen checks now assert the tool list against the documented nine,
  that a read is open and a write is refused without the token, that a wrong
  token is not enough, that `LLM_API_REQUIRE_AUTH=1` closes the reads too, and
  that the transport still answers 421 to an unknown `Host`. Driven in-process
  through fastapi's `TestClient` with raw JSON-RPC, so it costs no subprocess.
  Verified by mutation: disabling the gate turns five of the fourteen red.
- **The control page is built on a design system.**
  [Stellar DS](https://github.com/Polygonschmiede/stellar-ds)
  (`@polygonschmied/stellar-tokens`), vendored as two CSS files under
  `web/vendor/stellar` and served from the same origin — so still no build step
  and still nothing fetched from anywhere but this server. The page's own style
  block went from 85 hand-written lines to 45: it had nine colour tokens, five
  unrelated radii, eight font sizes off no scale, and not one shadow or
  transition in the whole file. What is left is what the system does not cover —
  the dense tables, the sticky header, the `hidden` guard, the glossary. Cards,
  buttons, tags, banners, meters, tabs, fields and the dialog are now the
  system's own components. Dark mode follows the OS with no switch to find, and
  `color-scheme` is finally declared, so the checkbox and the `select` popup stop
  staying light while everything around them turns dark.
- **Every abbreviation on the page explains itself on hover.** `Q4_K_M`, `-kvu`,
  `q8_0`, `gfx1201`, `ttl`, `spillover`, `warm`, `persistent`, junction
  temperature, the two card numberings — and **every flag inside a command line,
  a macro body or a dry-run diff**, which is the only place those flags are
  visible at all. The wording is this repository's own (`FLAGS.md`, `MODELS.md`,
  `API.md`, the docstrings in `lib/llmreg.py`) cut to a sentence each, so the
  page and the docs cannot drift apart. Quant names are generated rather than
  listed, so `UD-Q5_K_XL` gets a sentence too, and a term the page does not know
  renders as plain text instead of an empty tooltip. The eight `title` attributes
  that existed before are folded into the same mechanism, so the browser cannot
  draw its own tooltip on top of the styled one. Underlined terms are reachable
  by keyboard; the flags inside a `<pre>` are hover-only on purpose.
- `GET /ui/{asset}` serves the page's stylesheets from a fixed table of names —
  not a directory joined onto a path parameter, in a process that can read every
  model file and the API token. It answers `If-None-Match` itself, because
  `FileResponse` sends an ETag but does not act on one, and without that the
  browser would re-fetch 78 KB of unchanged CSS on every page load.
- `tests/ui-matrix.sh` now checks that every class and token the page asks for
  still exists in the vendored CSS. That is the failure a design-system upgrade
  actually produces: a renamed class, and a page that silently falls back to
  unstyled.
- **Power draw and utilisation per card**, next to the junction temperature they
  arrive with — in `GET /api/gpus` as `powerW` and `busyPercent`, in the Cards
  tab with an explanation each, and in the card table that `llm status`,
  `llm gpu list` and `llm watch` all print. One `rocm-smi` query, and a sensor
  the driver does not answer reads `?` rather than a confident zero. A discrete
  card calls it "Average Graphics Package Power" and an APU "Current Socket
  Graphics Package Power", so both labels are accepted.
- **`llm watch` shows this stack's view instead of raw `rocm-smi`.** It was
  `watch -n 2 rocm-smi`: every device the driver exposes, the integrated GPU
  included, and nothing about what the stack was doing with them. Now it is the
  model that is loaded plus the same filtered card table as `llm status`.

### Changed

- **The registry runs on `mcp` 2.x — and only on 2.x.** The 2.0 release renamed
  `mcp.server.fastmcp` to `mcp.server.mcpserver` (`FastMCP` is now `MCPServer`),
  removed the module-wide `get_context()`, and moved `stateless_http`,
  `streamable_http_path` and `transport_security` out of the constructor into
  `streamable_http_app()`. `bin/llm-api.py` used all three, so there is no
  version range that covers both series and the pin had been sitting at `<2` with
  a comment explaining why. The migration: each of the nine tools now takes a
  keyword-only `ctx: Context`, which mcp injects by type hint and keeps out of the
  tool schema — verified tool by tool, so a client sees exactly the arguments it
  saw before. The token the write gate checks comes from `ctx.headers` instead of
  a global lookup. `mcp.session_manager` raises in 2.0 until
  `streamable_http_app()` has run once, so the ASGI app is built next to the
  server object rather than at the mount, where a later edit could reorder it.
  **An existing installation needs `llm setup` once** to move its `venv-api`;
  until it does, the registry will not start. `llm doctor` tests the new import
  and now flags 1.x as too old. Dependabot's `ignore` on major `mcp` updates goes
  with it: it was there because the import had not been migrated, and a rule that
  outlives its reason only hides the next one of these.

### Fixed

- **The pi extension's event stream sent no token.** `/api/events` counts as a
  read, and reads need the token once the registry runs with
  `LLM_API_REQUIRE_AUTH=1` — but the watcher called `fetch` directly instead of
  going through the helper that sets `X-LLM-Token`. The 401 landed in an empty
  `catch`, so the client reconnected every fifteen seconds forever and the
  catalog silently stopped following the server: no message, no failure, just a
  session working from a state that quietly aged. The header is sent now, the
  configuration is re-read per attempt so a token added after startup takes
  effect, and a 401 is reported once instead of never.
- **The extension never read the inference key, while the documentation said it
  did.** `apiKey` was the literal `sk-local` and `GET /api/pi-models.json` was
  never called, so with `llm key` on the provider sent a value the endpoint had
  stopped accepting and every completion came back 401 — from port 8080, with a
  model list that looked perfectly healthy. The key now comes from the registry
  along with everything else. `/api/health` stays a separate request on purpose:
  it never needs a token, so a client without one still learns the right address
  instead of falling back to its own loopback. Documented with the limit that
  actually applies — pi takes the key when the provider is registered, so a
  rotation on the server needs a new session on the client, not just a refresh.
- **A 401 was reported as "registry not reachable".** The server had answered —
  it just wanted a token — and the wording sent people to look at their firewall
  instead. It now says which of the two it is, and names the address from the
  configuration as it stands rather than as it stood when pi started.
- **`llm api client` and the setup guide named only half the leftovers.** A client
  configured by hand before the extension existed keeps `defaultProvider` and
  `defaultModel` in `~/.pi/agent/settings.json`, and those two survive the deleted
  `models.json` — so pi goes on pointing at a provider that no longer exists while
  the registry's catalogue sits unused right next to it. Both the printout and
  step 5 of `docs/PI.md` say so now.
- **An expanded model closed itself again after fifteen seconds.** The refresh
  re-renders the whole tab and the detail body was built with `hidden` set every
  time, so the state was thrown away on the next tick — which looked like a
  stray event handler and was a re-render forgetting what it had. Now recorded,
  with a test that clicks *details*, triggers the refresh and fails if the body
  shut itself.
- **Two test suites had never actually run against a fresh checkout.**
  `gpu-matrix` and `vram-matrix` probed `llmreg` without setting `LLM_HOME`, so
  they read whatever `config/llama-swap.yaml` happened to be in the working
  copy. That file is gitignored — it exists on a machine that uses this stack and
  does not exist in a clone, where every function touching the config raises
  `ConfigMissing`. Seventeen and nine checks respectively passed locally for that
  reason and failed the first time CI ran them. The probes now share one
  throwaway `LLM_HOME`, which also makes them deterministic.
- `tests/dom-stub.js` read `hidden` as false whenever it had been set the way
  `el()` sets it (`setAttribute("hidden", "")`), so anything asserting on a
  collapsed element passed for the wrong reason. It also returned `this` from
  `closest()` unconditionally, which made every `closest(...)` in the page look
  like it worked.
- **`llm update whisper` was blocked for good.** The refusal "the whisper.cpp
  repository has local changes" was about `whisper.cpp/build` — the symlink this
  tool creates itself. Upstream's `.gitignore` writes `build/` with a trailing
  slash, which ignores the directory and not the symlink, so the guard counted
  the tool's own artifact as a local change. It now looks at **tracked files
  only**, the way the ComfyUI path already did; if an untracked file really is in
  the way, git says so with the path. The same guard on llama.cpp had the same
  hole and only escaped it because upstream happens to write `/build*` there.
- **The build itself made the repository dirty.** whisper.cpp's top-level
  `CMakeLists.txt` regenerates `bindings/javascript/package.json` in the source
  tree on every configure, so one build left a *tracked* modification and the
  fixed guard would have refused every later update anyway. Files a build writes
  back are now known, excluded from the guard and dropped before the checkout.
- **"Up to date" is decided by commit, not by tag name.** whisper.cpp publishes
  rolling `bNNNN` releases next to hand-cut `v1.x.y` ones, and GitHub's "latest
  release" answers with whichever came last and is not a prerelease — so
  `active v1.9.2` against `latest b4938` could be a real update or the same
  commit under the other name. `.update-cache` now stores the commit each latest
  tag points at and comparison uses it; when either side cannot be resolved, the
  name comparison stands and an update is offered. `llm update`, `llm status`,
  `GET /api/versions` and the control page share this.
- A successful `llm rollback whisper` reported itself as **failed**: the source
  checkout that follows the build directory is a courtesy, and its exit status
  was becoming the command's. It no longer is, and it says so when the source
  cannot follow.
- `GET /api/versions` reported ComfyUI with no active version at all, so its row
  had nothing to compare and no rollback to offer.
- Job logs no longer carry raw escape sequences. `bin/llm` honours `NO_COLOR`,
  the registry sets it for every job, and what other tools still emit is
  stripped. This affected the download log too.

- **The test runner reported a green result for a run that was less than half
  done.** `tests/api-matrix.sh` needs `venv-api` and `tests/ui-matrix.sh` needs
  `node`; without them each called `skip` and then `summary; exit $?`, and
  `summary` returned 0 as long as nothing had *failed*. So `run-all.sh` printed
  *"all 5 suites passed"* on a fresh clone while **130 of about 300 assertions
  had never executed**. The summary names the skipped count now, `--strict` turns
  a skip into a failure, and CI has a step that asserts `--strict` *fails*
  without those dependencies — so the old behaviour cannot return quietly. This
  was precisely what `CONTRIBUTING.md` calls the worst kind of test: one that
  reads like cover.
- **The inference key was readable by every account on the machine.** `llm key
  new` writes it to `config/api-key` with mode 600 and then, because llama-swap
  needs it there, a copy of it into `config/llama-swap.yaml` — which `llm init`
  created 644. The careful mode on the small file was undone by the large one
  beside it. Config writes narrow the file to 600 whenever it contains an
  `apiKeys:` block, at the single point every write passes through, and
  `llm doctor` reports an older installation that is still 644.
- **`llm update swap <version>` installed the newest release instead.** It read
  `releases/latest` whatever version was requested, downloaded that tarball and
  then reported itself as the version you had named. It asks for the release you
  named.
- **The one binary this project does not build was installed unverified.** The
  `llama-swap` tarball's only gate was that the extracted file answered
  `--version`, which a substituted archive would also do. Its SHA-256 is checked
  against the checksum list published in the same release, before unpacking, and
  a release without such a list is refused rather than trusted. Verified both
  ways: the real archive passes, a tampered one is rejected with both digests
  printed.
- **`LLM_API_REQUIRE_AUTH=1` left half the door open.** It closed reads over
  HTTP while the MCP tools `list_models`, `get_model`, `gpu_status` and
  `job_status` kept answering without a token — the same catalog and the same
  filesystem paths, through `/mcp` instead of `/api`. They follow the switch now.
  The MCP transport's `Origin` check also accepted `*`; it gets the same list as
  the `Host` check, which is what makes that DNS-rebinding protection whole.
- **The registry created no write token unless it was started by its own
  `__main__`.** `api_token(create=True)` sat under `if __name__ == "__main__"`,
  so a process started through an ASGI server answered 503 "no token configured"
  to every write while `llm api token` printed one — created by the CLI, not by
  the service. It moved into the lifespan handler.
- Token comparison is `secrets.compare_digest` rather than `==`, in all four
  places that compare one.
- A failure to write `config/api-key.env` was swallowed by a bare
  `except OSError: pass`, so the chat UI kept its previous key and began failing
  401 after the next restart with nothing saying why. It warns — and the warning
  names neither `API_KEY_ENV` nor the exception object — CodeQL reads a name
  containing "key" as a secret, and neither form could have carried the key
  anyway. It was the new scanner's first finding, on code from the pull request
  that added it.
- The snippet `llm api client` prints created `~/.pi/agent/llm-box.json` — which
  holds the registry write token — with whatever umask the client machine had. It
  creates the directory and uses `umask 077`.
- **The `docs` job had never checked a link.** It was a python heredoc in the
  workflow — `git ls-files '*.md' | python - <<'PY'` — and the heredoc *is*
  stdin, so the interpreter read its program from there and the piped file list
  went nowhere. `for line in sys.stdin` iterated an exhausted stream, found
  nothing, and the job reported success for every push since it was written.
  Confirmed by putting a broken link in and watching it exit 0. It is
  `tests/check-links.py` now, taking the file list as arguments, and it is also a
  check in `tests/repo-matrix.sh` so it runs locally. All 50 relative links in
  the tree do resolve — the documentation was fine, the check was not.
- **shellcheck never saw `lib/update.sh`.** The workflow named four paths, and
  636 lines that build engines and restart services were reached only
  transitively through `-x`. The file list comes from `git ls-files`, so a new
  script cannot be forgotten.
- Three stale claims in `docs/UPDATES.md`: the build flags live in
  `lib/update.sh` rather than `bin/llm`, and `GGML_CCACHE=ON` is llama.cpp's own
  default rather than something this project sets.

- **`llm update llama` moved the source tree before checking it could build.**
  The backend prerequisites were validated at the cmake call, by which point
  `git fetch` and `git checkout <tag>` had already run — so a machine missing
  `glslc` was left with a checkout on a version nothing was built from. The
  active build was never at risk, which is what the versioned build directories
  are for. The check runs before the fetch now, for llama.cpp and whisper.cpp.
- The Vulkan build prerequisite check was wrong twice before it was right, and
  both ways are worth recording: looking for `/usr/include/vulkan/vulkan.h`
  fails on a hand-installed SDK, and preprocessing that header succeeds while
  cmake's `find_package(Vulkan)` still fails for want of the link-time library.
  It compiles and links now, and covers `spirv-headers` too — determined by
  building it: leaving any of the three packages out fails at configure. It also
  *runs* `glslc` rather than merely finding it — ggml probes shader extensions by looking for "extension not supported" in
  glslc's output and treats anything else as supported, so a `glslc` that cannot
  start makes every extension look available and the build dies thousands of
  shader lines later.

- **`GET /api/gpus` and `GET /api/state` answered a fresh clone with an HTTP 500.**
  `ConfigMissing` was converted to a 503 route by route, five of them did it, and
  those two were not among them — so the control page's own card fetch returned a
  traceback on every installation that had not run `llm init` yet. One app-level
  exception handler covers every route now, and the five hand-written copies are
  gone. The test that should have caught it asked only about `/api/models`, and
  only that it was *not* a 500, which 200, 401, 404 and 422 also satisfy; it now
  loops over every read route and asserts 503 plus a message naming `llm init`.
- **`llm status` never mentioned a missing configuration.** It is the command you
  run to find out where you are, and on a fresh installation it printed the
  services and versions and said nothing about the one thing standing between you
  and a working endpoint. That warning existed only in `llm setup`. It still exits
  0 — a status command should report, not refuse — but it says it.
- **`"source": "manuell"` — a German string in every sidecar** that
  `llm meta backfill --repo` wrote, with no umlaut to give it away, which is why
  the language guard's word list never saw it. Fixed, and the word is in the list
  now; that list growing is what it is for.
- Three checks in the shell suites that could not fail as written:
  `tests/vram-matrix.sh` ran sixteen KV-cache checks with no `LLM_HOME`, so
  `llmreg` bound to the real checkout — harmless today because those read no
  file, and exactly the leak `tests/lib.sh` documents as having burned this
  project once; `tests/api-matrix.sh` did not set `LLM_SWAP_API` either, so on a
  machine running this stack it queried the **real** llama-swap; and
  `tests/ui-matrix.sh` compared a stylesheet check against the empty string,
  which also passes when the extraction finds nothing at all.
- **The eight committed `rocm-smi` fixtures were free to rot.** The harness
  regenerates into a temporary directory, so nothing ever read the copies in the
  tree — change the generator and they quietly become a different machine from
  the one under test. `tests/repo-matrix.sh` now holds them to their generator in
  both directions. The one fixture that was carved out of another with
  `sed -n '4,$p' | head -n -2`, tied to the exact header and footer line count,
  is a case in the generator instead.
- `LLM_TENSOR_SPLIT` was written into `config/hardware.env` and read by nothing —
  not `bin/llm`, not `lib/update.sh`, and not llama.cpp, which knows no such
  name. The units export that file into every service, so a dead variable is not
  free. Removed; `tensorSplit` is still in the `hw()` payload, where the API and
  `llm doctor` read it from.

### Added

- **Updates and rollbacks from the control page.** The System tab now has
  *check now*, *update everything*, and per engine *update* and *back*. Each one
  says what it is about to do — a build takes tens of minutes, the endpoint is
  away for a few seconds, a smoke test decides whether the new version becomes
  active at all — and then runs as a job whose log you can watch. Downloads and
  updates are listed on their own tabs rather than in one pile.
- `POST /api/updates/check`, `POST /api/updates/{component}` and
  `POST /api/rollback/{component}`, through the same job runner the downloads
  use. Writes, so they need the token or a session; the component is checked
  against a fixed list because it becomes part of a command line; and only one
  runs at a time — a second gets 409, since they share the repositories, the
  build symlinks and the services. Until now the page could show a stale version
  and do nothing about it, and there was no way at all to refresh the cache
  without a shell.
- whisper.cpp got the parts llama.cpp already had: the health check with
  automatic revert when the endpoint stops answering after a switch, pruning of
  old builds (they used to accumulate without limit), and the free-disk check.
- `LLM_WHISPER_HOME` overrides where whisper.cpp lives, the way
  `LLM_COMFY_HOME` already did — which also lets the page tests run against a
  sandbox instead of whatever the machine has installed.

- **Six CI jobs instead of three, and every tool version pinned.** `gitleaks`
  over the whole commit history, `pip-audit` on the registry's requirements,
  `zizmor` on the workflows themselves, and CodeQL for Python and
  JavaScript/TypeScript in its own workflow. Actions are pinned by commit hash,
  linters by version, and Dependabot moves both weekly — pinning without updates
  is just old software with extra steps. The unpinned versions were not
  hypothetical: the runner image shipped shellcheck 0.9.0 while the author had
  0.11.0, and CI went red on a tree that was green locally.
- **`tests/update-matrix.sh`** — 33 checks on `lib/update.sh`, which had none.
  636 lines that build engines and restart services, covered until now by
  shellcheck and nothing else. Reachable because `bin/llm` *sources* that file:
  the suite fakes the six helpers it reaches for and calls the pure functions
  directly against empty directories in `/tmp`. No build, no network, no GPU.
  It covers the build-directory naming, which builds belong to which backend,
  what the symlink says is active, what prune keeps, the version cache, and the
  dirty guard in all four of its cases. Verified by breaking two things
  deliberately — and one of those breakages showed prune deleting the other
  backend's builds, which is the silent kind of damage this exists to prevent.
- **`tests/repo-matrix.sh`** — the four checks that are about the repository
  rather than the code: no leftover German, no machine-specific path or private
  address, nothing per-machine or downloaded tracked, and one version number
  rather than three (`VERSION`, `package.json`, `CHANGELOG.md`) that agreed by
  hand. Three of these were inline in the workflow, where a contributor could
  only find them by pushing and going red. The secrets check asks `git ls-files`
  rather than the filesystem, so loosening `.gitignore` cannot quiet it.
- **`bash tests/run-all.sh --strict`**, and a summary of what actually ran rather
  than how many suites returned zero. See *Fixed* for why that is not a nicety.
- **Issue and pull request templates, `CODEOWNERS`, and a private security
  channel.** `SECURITY.md` said "open an issue"; reports go through GitHub
  Security Advisories now. The hardware template asks for raw `rocm-smi` output,
  because "which card, which ROCm version" is always the first question and
  `tests/fixtures/` exists so a machine nobody here owns can be reproduced.

- **A Vulkan backend, chosen at setup.** `llm init --backend vulkan` (or
  `llm gpu backend vulkan` later) builds llama.cpp and whisper.cpp with
  `-DGGML_VULKAN=ON` instead of HIP. It exists because `docs/INSTALL.md` used to
  begin "from a bare ROCm machine": an AMD card ROCm has dropped, an Intel Arc or
  an NVIDIA card had no way in at all. Vulkan needs no `AMDGPU_TARGETS` and no
  HIP compiler — one cmake flag and three distribution packages — so it also
  cannot be built for hardware it has not met. ROCm stays the default wherever it
  is complete, because it is faster where it works.

  What this touches, and what it does not:

  - **`lib/gpu_rocm.py` and `lib/gpu_vulkan.py`** — the only split out of
    `lib/llmreg.py`, and it exists because the card detection would otherwise be
    written twice. Everything above it is shared: the absolute↔logical
    translation, the `LLM_DGPUS` override, the fit arithmetic.
  - **Card readings under Vulkan** come from `vulkaninfo` for identity and order
    and from amdgpu's sysfs for temperature, watts, utilisation and VRAM, joined
    by the DRM card number the driver reports rather than by list position.
    Cross-checked against `rocm-smi` on the same two cards: identical VRAM to the
    byte and identical junction temperatures, with power and utilisation differing
    only by the seconds between the two samples.
  - **`discrete` is now a fact, not a guess.** Vulkan states the device type, so
    the integrated GPU — and `llvmpipe`, the software rasteriser Vulkan offers on
    every mesa machine and nobody wants a model on — are excluded outright.
    Under ROCm the same distinction still has to be inferred from a CPU brand name
    and a VRAM threshold.
  - **A card the driver says nothing about is still a card.** On a non-amdgpu
    driver those sensors are absent; the card is detected, placed and counted, and
    shows `?`. `check_fit` answers "the fit was not checked" instead of refusing —
    treating absent VRAM as zero would have blocked every model on such a card
    while claiming it had "0.0 GB free".
  - **`config/hardware.env`** carries `LLM_BACKEND`, and the visible-devices mask
    under its own name — `HIP_VISIBLE_DEVICES` or `GGML_VK_VISIBLE_DEVICES`, never
    both.
  - **A switch rewrites the configuration**: `--device ROCm0` becomes
    `--device Vulkan0`, and a whisper entry's mask is renamed too, which is not
    cosmetic — whisper has no `--device` flag, and a mask the runtime does not read
    is not a mask. Reading always accepts either spelling, so a config survives a
    switch in both directions.
  - **Switching back and forth is a symlink change**, not a rebuild each time.
    The backend is in the build directory's *name* — `build-b10545` for ROCm
    (unchanged, so existing installations are untouched) and
    `build-vulkan-b10545` beside it — so both can exist at once and
    `llm gpu backend` relinks where a build is already there. Only the first
    build of each backend costs a build. `KEEP_BUILDS` and `llm versions` are
    scoped per backend, so a rollback never silently changes backend and pruning
    one backend cannot delete the other's fallbacks — which is what the first
    attempt at this would have done, and what `tests/update-matrix.sh` now
    catches.
  - **ComfyUI stays ROCm-only** and says so instead of downloading three gigabytes
    of PyTorch wheel and then failing on `torch.version.hip`. There is no Vulkan
    build of PyTorch.
  - `llm doctor` checks the tools of the backend in force rather than both, names
    it, and reports the one failure that looks like nothing else: llama.cpp seeing
    zero devices because the build is for the other backend.
  - **Vulkan turned out to be faster here, not slower.** Measured on one R9700
    with Qwen3.5-4B-Q4_K_M, 200-token generations, three runs each: 111.9 · 114.2
    · 113.8 tok/s under Vulkan against 97.7 · 99.4 · 99.5 under ROCm 7.1 — about
    14 % ahead. One small dense model on one card, generation only, so the docs
    say exactly that rather than turning it into a ranking. The first draft of
    those docs claimed Vulkan was slower; it was corrected after measuring.
  - 47 new checks in `tests/gpu-matrix.sh` against `tests/fixtures/mk-vulkan.py`,
    which fakes both sources. The expectations are the **same numbers** as the
    ROCm ones — that is the claim being tested — and the fixtures are hostile on
    purpose: the DRM card minors are permuted, so pairing the two device lists by
    position instead of by the reported minor turns seven checks red, and one
    fixture puts the software rasteriser FIRST, because ggml skips CPU-type
    devices and counting them would shift every index against what
    `--device VulkanN` means. Verified by breaking four things deliberately and
    watching the suite catch each.

- **`tests/unit/` — pytest, 67 checks on `lib/llmreg.py`.** The bash harness
  compares strings, and a lot of what that file does is not a string: a function
  whose answer is a raised exception, a module reimported with different
  environment, a GGUF header written byte by byte, a fake HTTP server standing in
  for llama-swap. Twenty-two of its functions were reached by no test at all -
  `gpu_sync`, `write_env`, `record_add`, `backfill`, `load_model`, `unload_all`,
  `file_digest` among them - and the provenance path is what `.gitignore` calls
  the only thing in the tree that cannot simply be downloaded again.

  `bash tests/run-all.sh` still needs nothing installed; the pytest suite skips
  itself, which `--strict` then counts as a failure. `config/requirements-dev.txt`
  is what `--strict` costs. CI prints a coverage figure for `lib/` as a **number
  and not a gate** — a threshold invites tests that move it rather than tests that
  would catch something. It stands at 61 % from the unit tests alone, and says out
  loud that `bin/` and `web/` are bash and inline JavaScript that coverage.py
  cannot see.
- **`tests/cli-matrix.sh` — 37 checks on `bin/llm`,** which had none. Two halves:
  the pure helpers called directly, and the command surface run as a real
  subprocess against a throwaway installation. Service control is deliberately
  left out; starting systemd units is not something a test should do to a machine.
- **`bin/llm` dispatches nothing when sourced**, the seam `lib/update.sh` already
  had. Executed, nothing changes. It is the difference between 1200 lines covered
  by shellcheck and 1200 lines with tests.

## [1.3.0] — 2026-08-20

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
- **`llm key`** puts an API key in front of port 8080, which llama-swap
  otherwise leaves default-allow. With one in force every path except `/health`
  needs `Authorization: Bearer <key>` — measured: `/v1/models`, `/unload`,
  `/ui`, `/logs`, `/metrics` and `/running` all answer 401 without it. That
  closes the mutating `GET /unload`, the open playground and the log viewer
  without moving the port to loopback, so remote inference keeps working. Off by
  default: turning it on breaks any client that does not know the key yet, so it
  has to be asked for.
- `llm doctor` reports the combination that actually matters — port 8080
  answering on a non-loopback address with no key in force — and names the fix.
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

- **`GET /api/pi-models.json` would have leaked the inference key.** It is the
  one read that stays open so a client can bootstrap itself, and its payload
  carries the key — so anyone who could reach port 8081 could have read the key
  that was supposed to protect port 8080. It now requires the registry token
  exactly when a key is set, and stays open when there is none.
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

[1.3.0]: https://github.com/Polygonschmiede/llm-box/releases/tag/v1.3.0
[1.2.0]: https://github.com/Polygonschmiede/llm-box/releases/tag/v1.2.0
[1.1.0]: https://github.com/Polygonschmiede/llm-box/releases/tag/v1.1.0
[1.0.0]: https://github.com/Polygonschmiede/llm-box/releases/tag/v1.0.0
