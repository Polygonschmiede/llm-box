## What this changes

<!-- What was wrong, and how you know it is fixed. Measured numbers where there
     are any - see CONTRIBUTING.md on commit bodies. -->

## How it was verified

<!-- Not "the tests pass" but which ones, and what you broke on purpose to
     confirm the suite goes red. CONTRIBUTING.md: a test that cannot fail is
     worse than no test. -->

- [ ] `bash tests/run-all.sh --strict` is green (no skips - if a suite skipped,
      say which and why)
- [ ] `ruff check .` and `shellcheck -x -S warning $(git ls-files '*.sh' bin/llm)`
      clean, at the versions pinned in `.github/workflows/ci.yml`
- [ ] Exercised on real hardware, if it touches the GPU, the build or a service

## Paperwork

- [ ] `CHANGELOG.md` entry under `## [Unreleased]`, if anything is user-visible
- [ ] Docs updated where behaviour changed - `docs/FLAGS.md` numbers go stale
      silently
- [ ] Nothing machine-specific and nothing generated is committed
      (`tests/repo-matrix.sh` checks this)
