# Contributing

## Running the tests

```bash
bash tests/run-all.sh            # everything that can run here
bash tests/run-all.sh --strict   # and a skipped check counts as a failure
```

The default run needs nothing installed. **None of it
needs a GPU and none of it touches your configuration.** The card detection is
fed fake `rocm-smi` output from `tests/fixtures/`, and everything that reads or
writes `llama-swap.yaml` runs against a temporary `LLM_HOME` in `/tmp`. It is
safe to run on a live server.

| suite | what it holds down |
|---|---|
| `tests/gpu-matrix.sh` | card detection, and the absolute↔logical translation in both directions |
| `tests/config-matrix.sh` | marker blocks, card groups, roles, `patch_model`, the flag primitives |
| `tests/vram-matrix.sh` | KV cache size for the three layouts, and the per-card fit |
| `tests/api-matrix.sh` | the registry's HTTP surface, both catalog shapes, auth |
| `tests/mcp-matrix.sh` | the registry's MCP surface: the tool list, and the token gate on reads and writes |
| `tests/ui-matrix.sh` | that `web/index.html` really renders, and fetches nothing external |
| `tests/update-matrix.sh` | `lib/update.sh`: build-directory naming per backend, what is active, what prune keeps, the dirty guard |
| `tests/cli-matrix.sh` | `bin/llm`: the pure helpers, and what each command says and exits with when something is missing |
| `tests/unit-matrix.sh` | a wrapper around `tests/unit/`, the pytest half |
| `tests/repo-matrix.sh` | the repository itself: no German, no machine paths, no tracked secret, generated files current, links resolve, one version number |

The runner finds suites by glob (`tests/*-matrix.sh`), so a new one needs no
edit anywhere. Suites run in their own process; the totals come back through
`LLM_TESTS_COUNTS`.

### Skipped is not passed

`tests/api-matrix.sh` and `tests/mcp-matrix.sh` need `venv-api`, and
`tests/ui-matrix.sh` needs `node`.
Without them those suites **skip themselves whole** — 130 of the ~300 checks.
They used to call `skip` and then exit 0, so the runner printed
*"all 5 suites passed"* on a machine where less than half of them had run. Now
the summary names the skipped count, and `--strict` turns it into a failure. Use
`--strict` before opening a pull request; CI runs it that way, and it also has a
step that asserts `--strict` **fails** without those dependencies, so the old
behaviour cannot come back quietly.

`--strict` costs three things: `node`, `venv-api` (`bash bin/llm setup`) and
pytest (`uv pip install --python venv-api/bin/python -r config/requirements-dev.txt`).
Everything skips the same way — it runs
the page's script under a minimal DOM (`tests/dom-stub.js`) against payloads
generated from a throwaway `LLM_HOME`. Curling `/ui` would not do: it answers
200 whatever the JavaScript does, and the first version of that check passed
while every element on the page read `[object Object]`.

Individual suites run on their own (`bash tests/config-matrix.sh`), which is
what you want while working on one area.

### Adding a check

`tests/lib.sh` is the shared harness:

- `check <name> <expected> <actual>` — plain string comparison.
- `check_err <name> <ExceptionName> <actual>` — for the cases where the point is
  *that* it refuses, so rewording an error message does not break a test.
- `probe <fixture> <expression>` — evaluate against a faked machine.
- `pyx <statements>` — run against `lib/llmreg.py` with the ambient environment.
- `sandbox [template]` — a throwaway `LLM_HOME`; prints its path.
- `add_block <home> <name> <cmd> [body]` — append a model's marker block.

A crash inside `pyx` reports the exception and prints the traceback, rather than
presenting as an ordinary mismatch.

**The check that matters most: break the thing on purpose and confirm the suite
goes red.** Every fix in `CHANGELOG.md` 1.2.0 was verified that way. A test that
cannot fail is worse than no test, because it reads like cover. Two of the first
drafts here passed against the bug they were written for — one asserted on a
returned note instead of what was written to the file.

## Linting

```bash
uvx ruff@0.16.4 check .
uvx --from shellcheck-py==0.11.0.1 shellcheck -x -S warning $(git ls-files '*.sh' bin/llm)
```

Both must be clean, and **the versions are the point**. These were `ruff@latest`
and whatever `shellcheck` the runner image shipped, which is how a tree that was
green locally went red in CI: the runner had shellcheck 0.9.0 and the author had
0.11.0, and only one of them saw an SC2120. The pins live in
`.github/workflows/ci.yml` under `env:` — if you move one, move it here too.

The file list is derived rather than written out for the same reason: the old
list named four paths and therefore never checked `lib/update.sh`, which is 636
lines that build engines and restart services. The ruff rule set in
`pyproject.toml` is deliberately about mistakes and not taste — the pathlib and
f-string families are off on purpose, because this codebase consistently uses
`os.path` and `%`-formatting and switching would be a rewrite for no behaviour
change. If a rule is wrong for a specific line, a `# noqa` **with a reason** next
to it is preferred over a file-wide ignore.

## Two harnesses, and which to use

`tests/*-matrix.sh` is bash: `check <name> <expected> <actual>` against a string.
`tests/unit/` is pytest, for what a string comparison cannot express - a function
whose answer is a raised exception, a module reimported with different
environment, a fake HTTP server standing in for llama-swap, a GGUF header written
byte by byte.

Reach for pytest when the assertion is about a **type or a shape**, and for bash
when it is about **a command's output or exit status**. Both run under
`bash tests/run-all.sh`; the totals it prints include the pytest count.

Three traps that have each cost time here, all of them mine:

- **llmreg reads its environment at IMPORT time.** `LLM_HOME`, `LLM_ROCM_SMI`,
  `LLM_VULKANINFO`, `LLM_SYSFS_ROOT` and `LLM_BACKEND` are module-level. A
  `monkeypatch.setenv` in the test body is too late, which is why the fixture is
  `load(**env)` and not a module handed to you. It is also why every bash probe
  runs a fresh interpreter.
- **`LLM_HOME` is the installation directory, not just the configuration.**
  `bin/llm` finds `lib/update.sh`, `lib/llmreg.py`, `VERSION` and the example
  config under it too, so pointing it at a bare temporary directory breaks the
  script at its first `source`. Use `cli_home` rather than `sandbox` for anything
  that runs the CLI.
- **`set -o pipefail` and `cmd | grep -q`.** The pipeline reports the failure of
  any member, so testing that a command which *refuses* also *says* something
  cannot be written that way. `says <pattern> <cmd...>` in `cli-matrix.sh`
  captures first. Six checks read as "the message is missing" when the message
  was there.

## What CI checks beyond that

Six jobs in `.github/workflows/ci.yml` plus CodeQL in its own workflow. Two of
them exist because of mistakes that already happened here, and it is worth
knowing which:

| job | what it holds down |
|---|---|
| `lint` | ruff, shellcheck, and `tests/repo-matrix.sh` |
| `test` | the suites under `--strict`, after first proving that `--strict` refuses an incomplete run; then a coverage figure for `lib/` as a number, not a gate |
| `docs` | `tests/check-links.py` — every relative markdown link resolves |
| `secrets` | `gitleaks` over the whole commit history, not just the tree |
| `deps` | `pip-audit` on `config/requirements-api.txt` only — Open WebUI's ~500 transitive packages are not this project's to pin |
| `workflows` | `zizmor` on the workflows themselves: unpinned actions, permissions wider than needed, injection through a PR title |
| `codeql` | Python and JavaScript/TypeScript. There is no extractor for bash, so `bin/llm` and `lib/update.sh` are covered by shellcheck and nothing else |

Everything is version-pinned, actions by commit hash. Dependabot moves those
pins weekly; that is what makes pinning something other than freezing.

**`gitleaks` runs over the history for a reason.** This repository generates
`config/api-token` and `config/api-key` per machine, and an agent worktree under
`.claude/` is a second checkout carrying a real one. `.gitignore` covers that
directory, and `tests/repo-matrix.sh` asks `git ls-files` — not the filesystem —
so relaxing an ignore rule cannot quiet the check.

## Where code belongs

`lib/llmreg.py` owns the truth: it reads and writes `llama-swap.yaml`, parses
GGUF headers, detects cards and computes VRAM. `bin/llm` is a front end, and
`bin/llm-api.py` is a second front end over HTTP and MCP. When bash greps the
YAML itself, that is a bug waiting — the two parsers drift apart, which has
already happened once with the `-ts` line.

`bin/llm` and `lib/update.sh` both **dispatch nothing when sourced**, so their
functions can be called directly from a test. Executed, nothing changes. That is
the seam `tests/cli-matrix.sh` and `tests/update-matrix.sh` use, and it is worth
keeping: it is the difference between 1800 lines of bash covered by shellcheck and
1800 lines with tests.

`lib/gpu_rocm.py` and `lib/gpu_vulkan.py` are the **only** split out of
`llmreg.py`, and it exists because that code would otherwise be written twice.
Each answers `cards()`, `gfx_targets()`, `compiler()`, `available()` and
`missing_hint()`, and declares `DEVICE_PREFIX` and `VISIBLE_ENV`. Everything
above them — the absolute↔logical translation, the `LLM_DGPUS` override, the fit
arithmetic, the config rewriting — stays in `llmreg.py`, written once. A new
backend is a third module and a name in `BACKENDS`; **if you find yourself
adding an `if backend == ...` outside those two modules and the dispatch
functions beside them, that is the sign the seam is in the wrong place.**

One contract detail worth keeping: a sensor the driver does not answer is an
**absent key**, never a zero. `?` in the table and `null` in JSON are honest; a
confident `0 W` is not. `check_fit` follows the same rule and answers "not
checked" rather than refusing a model on a card whose free VRAM cannot be read.

Two numbering spaces exist and mixing them up is the bug class this project
keeps hitting: the visible-devices mask (`HIP_VISIBLE_DEVICES`, or
`GGML_VK_VISIBLE_DEVICES` under Vulkan) uses **absolute** indices as the backend
counts them, while `--device ROCmN`/`--device VulkanN` and everything the API
reports use the **logical** position among the discrete cards. `to_smi()` and
`to_logical()` translate. Anything that writes a card number needs one of them.

`tests/fixtures/mk-vulkan.py` is hostile about this on purpose: it permutes the
DRM card minors, so pairing the Vulkan device list against sysfs by position
instead of by the reported minor reads the wrong card's temperature and seven
checks go red.

## Commits

Conventional Commits (`fix:`, `feat:`, `test:`, `ci:`, `docs:`, `chore:`). The
body is worth writing: the first two releases went in as two squashed commits of
~3700 lines each, so `git blame` cannot say why any individual line exists. Say
what was wrong and how you know it is fixed — measured numbers where there are
any.

Anything user-visible gets a `CHANGELOG.md` entry in the same commit.

## Documentation

Eight files in `docs/`, each with one job — `FLAGS.md` explains every
llama.cpp option this stack sets and why, `MODELS.md` covers choosing and
placing models, `API.md` and `PI.md` the agent surfaces. `tests/check-links.py`
checks that every relative link resolves — for real since this was a python
heredoc in the workflow that read its own program from stdin and therefore
checked nothing. If a change alters what a flag does, the number in the docs is
now wrong too.
