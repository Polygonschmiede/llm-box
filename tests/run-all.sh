#!/usr/bin/env bash
# ============================================================================
#  Run every suite in this directory
# ============================================================================
#  This is what CI calls, and what CONTRIBUTING.md points at. Nothing here
#  touches the machine's own configuration or its GPUs: the hardware is faked
#  through LLM_ROCM_SMI fixtures and the configuration through a temporary
#  LLM_HOME, so it is safe to run on a live server.
#
#  Run with:  bash tests/run-all.sh [--strict]
#
#  --strict makes a SKIPPED check a failure. Use it when you need the run to
#  mean "everything was checked" - in CI, or before opening a pull request. A
#  suite whose dependency is missing (node, venv-api) skips itself, and without
#  --strict that skip is only reported, not counted against the run.
# ============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1

strict=0
for a in "$@"; do
  case "$a" in
    --strict) strict=1;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) printf 'unknown option: %s\n' "$a" >&2; exit 2;;
  esac
done

#  Glob rather than a list: the list was a place to forget a new suite, and it
#  had already outlived one rename. Every suite is tests/<something>-matrix.sh;
#  lib.sh is the harness they source and is deliberately not matched.
suites=()
for f in tests/*-matrix.sh; do
  [[ -e "$f" ]] || continue
  suites+=("$(basename "$f" .sh)")
done
(( ${#suites[@]} > 0 )) || { echo "no suites found in tests/"; exit 1; }

#  Each suite is its own bash process, so the totals come back through a file.
counts="$(mktemp "${TMPDIR:-/tmp}/llm-counts.XXXXXX")"
trap 'rm -f "$counts"' EXIT
export LLM_TESTS_COUNTS="$counts"
(( strict )) && export LLM_TESTS_STRICT=1

failed=()
for s in "${suites[@]}"; do
  printf '\n\033[1;36m══ %s ══\033[0m\n' "$s"
  if bash "tests/$s.sh"; then :; else failed+=("$s"); fi
done

#  What actually ran, not just how many suites returned zero. The old summary
#  said "all 5 suites passed" while two of them had skipped themselves whole.
total_checks=0 total_fails=0 total_skips=0
while read -r c f s; do
  total_checks=$(( total_checks + c ))
  total_fails=$(( total_fails + f ))
  total_skips=$(( total_skips + s ))
done < "$counts"

printf '\n\033[1;36m══ result ══\033[0m\n'
printf '%d checks in %d suites' "$total_checks" "${#suites[@]}"
(( total_fails > 0 )) && printf ', \033[0;31m%d failed\033[0m' "$total_fails"
(( total_skips > 0 )) && printf ', \033[0;33m%d skipped\033[0m' "$total_skips"
printf '.\n'

if (( ${#failed[@]} > 0 )); then
  printf '\033[0;31mfailed: %s\033[0m\n' "${failed[*]}"
  exit 1
fi
if (( total_skips > 0 )); then
  #  Loud even without --strict: a run with skips is not the same statement as a
  #  run without them, and reading it as one is how 130 assertions went missing.
  printf '\033[0;33mincomplete: %d checks were skipped. --strict makes this a failure.\033[0m\n' \
    "$total_skips"
  (( strict )) && exit 1
  exit 0
fi
printf '\033[0;32mall %d suites passed, nothing skipped.\033[0m\n' "${#suites[@]}"
