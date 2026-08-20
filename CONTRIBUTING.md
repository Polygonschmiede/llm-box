# Contributing

## Running the tests

```bash
bash tests/run-all.sh
```

That is the whole story — no pytest, no npm, nothing to install. **None of it
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

`tests/api-matrix.sh` needs `venv-api`; without it those checks report as
**skipped** rather than failing. `bash bin/llm setup` builds it.

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
uvx ruff@latest check .
uvx --from shellcheck-py shellcheck -x -S warning bin/llm setup-system.sh bin/install-comfyui.sh tests/*.sh
```

Both must be clean; CI runs exactly these. The ruff rule set in
`pyproject.toml` is deliberately about mistakes and not taste — the pathlib and
f-string families are off on purpose, because this codebase consistently uses
`os.path` and `%`-formatting and switching would be a rewrite for no behaviour
change. If a rule is wrong for a specific line, a `# noqa` **with a reason** next
to it is preferred over a file-wide ignore.

## Where code belongs

`lib/llmreg.py` owns the truth: it reads and writes `llama-swap.yaml`, parses
GGUF headers, detects cards and computes VRAM. `bin/llm` is a front end, and
`bin/llm-api.py` is a second front end over HTTP and MCP. When bash greps the
YAML itself, that is a bug waiting — the two parsers drift apart, which has
already happened once with the `-ts` line.

Two numbering spaces exist and mixing them up is the bug class this project
keeps hitting: `HIP_VISIBLE_DEVICES` uses **absolute** indices as `rocm-smi`
counts them, `--device ROCmN` and everything the API reports use the **logical**
position among the discrete cards. `to_smi()` and `to_logical()` translate.
Anything that writes a card number needs one of them.

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
placing models, `API.md` and `PI.md` the agent surfaces. CI checks that every
relative link resolves. If a change alters what a flag does, the number in the
docs is now wrong too.
