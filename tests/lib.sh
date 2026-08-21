# shellcheck shell=bash
# ============================================================================
#  Shared harness for the test suites in this directory
# ============================================================================
#  Deliberately plain bash with no dependencies: the whole project installs
#  nothing beyond llama.cpp and two venvs, and a test suite that needed pytest
#  would be the only reason to add it.
#
#  The seam that makes this work is LLM_HOME (lib/llmreg.py): CONFIG, MODELS,
#  TOKEN_FILE and CONFIG_LOCK all derive from it, so pointing it at a temporary
#  directory exercises the real config read/write path without touching the
#  machine's own configuration. Every probe runs in a fresh interpreter, which
#  is what makes that safe - llmreg reads LLM_HOME at import time.
#
#  Source this, then call check/probe/pyx/sandbox and finish with summary.
# ============================================================================

REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/llm-tests.XXXXXX")"
FIXTURES="$TMP/fixtures"
trap 'rm -rf "$TMP"' EXIT

fails=0
checks=0

#  Fixtures are generated into TMP, never over the committed copies: the moment
#  mk-smi.py changes, regenerating in place would dirty the working tree on
#  every test run.
mkdir -p "$FIXTURES"
LLM_FIXTURE_DIR="$FIXTURES" python3 "$REPO/tests/fixtures/mk-smi.py" >/dev/null \
  || { echo "fixtures could not be generated"; exit 1; }

check(){ # $1=case  $2=expected  $3=actual
  checks=$((checks + 1))
  if [[ "$2" == "$3" ]]; then
    printf '  \033[0;32mok\033[0m    %-52s %s\n' "$1" "$3"
  else
    printf '  \033[0;31mFAIL\033[0m  %-52s expected %-20s got %s\n' "$1" "$2" "$3"
    fails=$((fails + 1))
  fi
}

#  Run python statements against lib/llmreg.py with the ambient environment.
#  A crash returns "ERROR: ..." rather than the last line of a traceback, so a
#  broken import presents as a broken test instead of a puzzling mismatch.
pyx(){ # $1=python statements
  local out rc
  out=$(python3 -c "
import sys
sys.path.insert(0, '$REPO/lib')
import llmreg
$1
" 2>"$TMP/py.err")
  rc=$?
  if (( rc != 0 )); then
    printf 'ERROR: %s' "$(grep -E '^\w*(Error|Exception|Exit)' "$TMP/py.err" | tail -1)"
    printf '\n\033[0;31m        traceback:\033[0m\n' >&2
    sed 's/^/        /' "$TMP/py.err" >&2
    return 1
  fi
  printf '%s' "$out"
}

#  Every probe runs against ONE throwaway LLM_HOME (see PROBE_HOME below).
#  Without it these ran against whatever config happened to be in the checkout -
#  which exists on a machine that uses this stack and does NOT exist in a fresh
#  clone, where llmreg raises ConfigMissing instead. Fifteen checks in
#  gpu-matrix and vram-matrix passed locally for exactly that reason and failed
#  the first time CI ran them.
probe(){ # $1=fixture  $2=expression -> one line
  LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$FIXTURES/rocm-smi-$1.sh" \
  LLM_DGPUS='' LLM_MIN_VRAM_GB='' \
    pyx "print($2)"
}

#  A throwaway LLM_HOME. Without an argument the config is minimal and easy to
#  reason about; "template" renders the shipped example the way 'llm init' does.
sandbox(){ # [template] -> prints the LLM_HOME path
  local home
  home="$(mktemp -d "$TMP/home.XXXXXX")"
  mkdir -p "$home/config" "$home/models"
  if [[ "${1:-}" == template ]]; then
    sed -e "s|@LLM_HOME@|$home|g" -e "s|@WHISPER_HOME@|$home/whisper.cpp|g" \
        "$REPO/config/llama-swap.example.yaml" > "$home/config/llama-swap.yaml"
  else
    cat > "$home/config/llama-swap.yaml" <<'YAMLEOF'
healthCheckTimeout: 300
logLevel: info

macros:
  server: >
    /bin/llama-server
    --host 127.0.0.1 --port ${PORT}
    -ngl 99 -fa on --no-webui --jinja
    -ts 1,1

# ============================================================================
#  MODELS  ('models' stays the LAST section)
# ============================================================================
models:
YAMLEOF
  fi
  printf '%s' "$home"
}

#  One sandbox for the read-only probes, so they never depend on the checkout.
#  Cheap: a mkdir and a heredoc. The empty argument is deliberate - it selects
#  the minimal config, and it is also the first call to sandbox() from inside
#  this file, which shellcheck would otherwise read as "the parameter is never
#  passed by anyone" (SC2120).
PROBE_HOME="$(sandbox "")"

#  Append a marker block for one model to a sandbox config. Only blocks with
#  markers are visible to parse_config, so a hand-added entry is invisible.
add_block(){ # $1=LLM_HOME  $2=name  $3=cmd  [$4=extra body lines]
  {
    printf '\n# >>> llm:%s\n  "%s":\n    cmd: "%s"\n    ttl: 900\n' "$2" "$2" "$3"
    [[ -n "${4:-}" ]] && printf '%s\n' "$4"
    printf '# <<< llm:%s\n' "$2"
  } >> "$1/config/llama-swap.yaml"
}

#  For the cases where the point is THAT it refuses, not the wording. Comparing
#  full messages would turn every reworded error into a failing test.
check_err(){ # $1=case  $2=expected exception name  $3=actual output
  checks=$((checks + 1))
  if [[ "$3" == "ERROR: $2:"* || "$3" == "ERROR: $2" ]]; then
    printf '  \033[0;32mok\033[0m    %-52s %s\n' "$1" "$2"
  else
    printf '  \033[0;31mFAIL\033[0m  %-52s expected %-20s got %s\n' "$1" "$2" "$3"
    fails=$((fails + 1))
  fi
}

#  Skipped, not failed: the registry suite needs venv-api, and a checkout that
#  has not run 'llm setup' should report that rather than go red.
skips=0
skip(){ # $1=case  $2=why
  skips=$((skips + 1))
  printf '  \033[0;33mskip\033[0m  %-52s %s\n' "$1" "$2"
}

#  Python from the registry environment (fastapi, mcp). Falls back to the system
#  interpreter, which is enough for anything that only imports llmreg.
PYAPI="$REPO/venv-api/bin/python"
[[ -x "$PYAPI" ]] || PYAPI="python3"

have_api(){ "$PYAPI" -c 'import fastapi.testclient, mcp' >/dev/null 2>&1; }

pyapi(){ # $1=python statements, run with the registry environment
  local out rc
  out=$("$PYAPI" -c "
import sys
sys.path.insert(0, '$REPO/lib')
$1
" 2>"$TMP/py.err")
  rc=$?
  if (( rc != 0 )); then
    printf 'ERROR: %s' "$(grep -E '^\w*(Error|Exception)' "$TMP/py.err" | tail -1)"
    printf '\n\033[0;31m        traceback:\033[0m\n' >&2
    sed 's/^/        /' "$TMP/py.err" >&2
    return 1
  fi
  printf '%s' "$out"
}

section(){ printf '\n\033[0;36m%s\033[0m\n' "$1"; }

summary(){
  local tail=""
  (( skips > 0 )) && tail="$(printf ', \033[0;33m%d skipped\033[0m' "$skips")"
  printf '\n'
  if (( fails == 0 )); then
    printf '\033[0;32m%d checks passed\033[0m%s.\n' "$checks" "$tail"
  else
    printf '\033[0;31m%d of %d checks failed\033[0m%s.\n' "$fails" "$checks" "$tail"
  fi
  return $(( fails > 0 ))
}
