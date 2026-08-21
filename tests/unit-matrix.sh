#!/usr/bin/env bash
# ============================================================================
#  The pytest half — lib/llmreg.py, tested directly
# ============================================================================
#  Everything the shell suites cannot reach without contortions: a function whose
#  answer is a raised exception, a module reimported with different environment,
#  a fake HTTP server standing in for llama-swap, a GGUF header written byte by
#  byte. `check` compares strings, and a lot of what llmreg does is not a string.
#
#  This is a thin wrapper so that `bash tests/run-all.sh` stays the one entry
#  point and the totals it prints include these tests. Without pytest it SKIPS,
#  which --strict then counts as a failure - see config/requirements-dev.txt.
#
#  Run with:  bash tests/unit-matrix.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
# shellcheck source=tests/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

#  The registry environment first: it is the one 'llm setup' builds, so a machine
#  that can run api-matrix is one install away from running this too.
PY=""
for cand in "$REPO/venv-api/bin/python" python3; do
  command -v "$cand" >/dev/null 2>&1 || [[ -x "$cand" ]] || continue
  if "$cand" -c 'import pytest' >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [[ -z "$PY" ]]; then
  skip "the unit tests" "pytest is not installed (config/requirements-dev.txt)"
  summary; exit $?
fi

out="$TMP/pytest.out"
#  -p no:cacheprovider: a .pytest_cache in the working tree would be one more
#  thing for the "nothing generated is committed" check to trip over.
"$PY" -m pytest -q -p no:cacheprovider --color=no "$REPO/tests/unit" > "$out" 2>&1
rc=$?
sed 's/^/  /' "$out"

#  Fold pytest's own count into the harness totals, so run-all.sh reports what
#  actually ran rather than "one suite passed" for sixty-odd assertions.
#  grep -o, not a sed with leading context: pytest's summary line begins with the
#  number ("67 passed in 6.20s"), so a pattern demanding a character before it
#  matches nothing and every run reports zero. That mistake has been made twice in
#  this repository now.
passed=$(grep -oE '[0-9]+ passed' "$out" | tail -1 | cut -d' ' -f1)
failed=$(grep -oE '[0-9]+ failed' "$out" | tail -1 | cut -d' ' -f1)
passed=${passed:-0}; failed=${failed:-0}
if (( rc != 0 && failed == 0 )); then
  #  A collection error, an import error, an internal crash: pytest exits
  #  non-zero with no "N failed" to parse. Counting that as zero failures is how
  #  a suite reports success for a run that never started.
  check "pytest ran at all" "exit 0" "exit $rc"
else
  checks=$(( checks + passed + failed ))
  fails=$(( fails + failed ))
fi

summary
