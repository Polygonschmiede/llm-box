#!/usr/bin/env bash
# ============================================================================
#  bin/llm — the front end, 1200 lines that had no executing test
# ============================================================================
#  Two halves, because the file has two kinds of thing in it:
#
#  * Pure helpers, called directly. bin/llm dispatches nothing when SOURCED, the
#    same seam lib/update.sh has always had, so its functions can be exercised
#    without spawning a command per assertion.
#  * The command surface, run as a real subprocess against a throwaway LLM_HOME.
#    That is what actually matters about a CLI: the exit status and what it says
#    when something is missing. Service control is deliberately NOT covered -
#    starting systemd units is not something a test should do to a machine.
#
#  Run with:  bash tests/cli-matrix.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
# shellcheck source=tests/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

section "the name a model gets from its repository and quant"

#  Sourced, so bin/llm defines its functions and dispatches nothing - the guard
#  at the bottom of the file compares BASH_SOURCE[0] with $0. $0 here is the
#  literal "llmfn", which is why no `set --` is needed: an earlier version of this
#  helper cleared the positional parameters to skip the dispatch and thereby
#  cleared the arguments it was trying to pass, so every call returned nothing and
#  two of these checks passed by comparing empty to empty.
llmfn(){ bash -c '. "$1"; shift; "$@"' llmfn "$REPO/bin/llm" "$@"; }

#  Capture, then grep. Under `set -o pipefail` a pipeline reports the failure of
#  ANY member, so `llm ls | grep -q x` is non-zero whenever llm ls exits non-zero
#  - which is exactly the case being tested here. Six checks read as "the message
#  is missing" when the message was there and the command had simply refused, as
#  it was supposed to.
says(){ # $1=pattern  $2...=command -> yes|no
  local pattern="$1"; shift
  local out; out="$("$@" 2>&1 || true)"
  grep -qiE "$pattern" <<<"$out" && echo yes || echo no
}

check "publisher dropped, suffix dropped, lowercased" "qwen3-8b-q4_k_m" \
  "$(llmfn mk_name unsloth/Qwen3-8B-GGUF Q4_K_M)"
check "the lowercase suffix too"                      "qwen3-8b-q4_k_m" \
  "$(llmfn mk_name unsloth/Qwen3-8B-gguf Q4_K_M)"
#  llama-swap model ids end up in URLs and in every client's config, so anything
#  that is not [a-z0-9._-] becomes a dash - and runs of dashes collapse, or
#  'Model  Name' would give a name with a hole in it.
check "spaces and punctuation become one dash"        "weird-name-q8_0" \
  "$(llmfn mk_name 'pub/Weird  Name!!' Q8_0)"
check "a trailing dash is trimmed"                    "model-f16" \
  "$(llmfn mk_name 'pub/Model-' F16)"
check "a repo without a publisher still works"        "solo-q4_k_m" \
  "$(llmfn mk_name Solo Q4_K_M)"

section "reading config/hardware.env"

HW="$(cli_home "")"
printf '# comment\nLLM_BACKEND=vulkan\nHIP_VISIBLE_DEVICES=0,1\nEMPTY=\n' \
  > "$HW/config/hardware.env"
hw(){ LLM_HOME="$HW" llmfn hw_get "$1"; }
check "a value comes back"           "vulkan" "$(hw LLM_BACKEND)"
check "one with a comma too"         "0,1"    "$(hw HIP_VISIBLE_DEVICES)"
check "an empty value is empty"      ""       "$(hw EMPTY)"
check "an absent key is empty"       ""       "$(hw NOT_THERE)"
#  A comment that happens to contain the key must not be read as the value.
printf '#LLM_BACKEND=rocm\nLLM_BACKEND=vulkan\n' > "$HW/config/hardware.env"
check "a commented-out line is not a value" "vulkan" "$(hw LLM_BACKEND)"

section "the commands that refuse, and what they say"

#  Every one of these is what a fresh clone hits, and the whole point of the
#  message is that it names the next command. Asserting on the exit code alone
#  would pass for a crash.
#  An installation with no configuration yet - which is what a clone is after
#  'sudo bash setup-system.sh' and before 'llm init'.
EMPTY_HOME="$(cli_home "")"
rm -f "$EMPTY_HOME/config/llama-swap.yaml"
llm(){ LLM_HOME="$EMPTY_HOME" NO_COLOR=1 bash "$REPO/bin/llm" "$@" 2>&1; }
rc(){ LLM_HOME="$EMPTY_HOME" NO_COLOR=1 bash "$REPO/bin/llm" "$@" >/dev/null 2>&1; echo $?; }

for cmd in ls role key; do
  check "llm $cmd without a config exits non-zero" "1" "$(rc "$cmd")"
  check "and names the command that fixes it" "yes" "$(says 'llm init' llm "$cmd")"
done
#  'llm status' is the exception on purpose: it is the command you run to find out
#  what state the machine is in, so it reports the missing configuration and exits
#  0 rather than refusing. Pinned here because the difference is deliberate.
check "llm status without a config still exits 0" "0" "$(rc status)"
check "and says what is missing"        "yes" "$(says 'llm init' llm status)"
check "an unknown command exits 2"      "2"   "$(rc nonsense)"
check "and names what it did not know"  "yes" "$(says 'unknown command: nonsense' llm nonsense)"
check "--version prints the VERSION file" "llm-box $(cat "$REPO/VERSION")" "$(llm --version)"
check "help exits 0"                    "0"   "$(rc help)"
check "and lists the backend command"   "yes" "$(says 'llm gpu backend' llm help)"

section "llm init"

check "it creates the configuration"  "yes" \
  "$(llm init >/dev/null 2>&1; [[ -f "$EMPTY_HOME/config/llama-swap.yaml" ]] && echo yes || echo no)"
check "with no placeholders left"     "0" \
  "$(grep -c '@LLM_HOME@\|@WHISPER_HOME@' "$EMPTY_HOME/config/llama-swap.yaml" || true)"
#  Refusing rather than overwriting: this file is the machine's model list, and
#  'llm init' is the command a confused person runs twice.
check "a second init refuses"         "1"   "$(rc init)"
check "and says it will not overwrite" "yes" "$(says 'not be overwritten' llm init)"
check "the config it wrote is readable by the service" "644" \
  "$(stat -c %a "$EMPTY_HOME/config/llama-swap.yaml")"
check "an unknown backend is refused"  "1" \
  "$(LLM_HOME="$(mktemp -d "$TMP/b.XXXXXX")" NO_COLOR=1 bash "$REPO/bin/llm" init --backend nonsense >/dev/null 2>&1; echo $?)"

section "llm ls against a configuration that has something in it"

H="$(cli_home "")"
add_block "$H" big  '${server} -m /m/big.gguf -c 8192 --device ROCm0 -sm none -mg 0'
add_block "$H" tiny '${server} -m /m/tiny.gguf -c 4096'
ls_out(){ LLM_HOME="$H" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" LLM_BACKEND=rocm \
          LLM_SWAP_API=http://127.0.0.1:9 NO_COLOR=1 bash "$REPO/bin/llm" "$@" 2>&1; }
out="$(ls_out ls)"
check "both models are listed"    "yes" \
  "$([[ "$out" == *big* && "$out" == *tiny* ]] && echo yes || echo no)"
check "llm ls exits 0"            "0" \
  "$(ls_out ls >/dev/null 2>&1; echo $?)"
#  llama-swap is not running in a test, and that has to read as a state rather
#  than as a broken command.
check "llm status survives a dead service" "0" \
  "$(ls_out status >/dev/null 2>&1; echo $?)"
check "and says so"               "yes" "$(says 'inactive|not running|failed' ls_out status)"

section "llm gpu"

check "the backend is reported"        "rocm" "$(ls_out gpu backend)"
check "an unknown backend is refused"  "1" \
  "$(ls_out gpu backend nonsense >/dev/null 2>&1; echo $?)"
check "the card table comes from the fixture" "yes" \
  "$(ls_out gpu list | grep -q 'card 0' && echo yes || echo no)"
check "a bad gpu subcommand says the usage" "yes" "$(says 'llm gpu' ls_out gpu nonsense)"

summary
