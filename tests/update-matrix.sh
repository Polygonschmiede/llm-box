#!/usr/bin/env bash
# ============================================================================
#  lib/update.sh — the part that decides WHICH binary becomes active
# ============================================================================
#  636 lines that build engines and restart services, and until now the only
#  thing that looked at them was shellcheck. This suite covers the pure
#  functions: the build-directory naming, which builds belong to which backend,
#  what the symlink says is active, and what prune keeps.
#
#  Reachable because bin/llm SOURCES that file rather than executing it, so the
#  functions can be called directly with the handful of helpers they reach for
#  faked out. Nothing here builds anything, touches the network or needs a GPU:
#  the "builds" are empty directories in a temporary tree.
#
#  Run with:  bash tests/update-matrix.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
# shellcheck source=tests/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

#  The helpers update.sh reaches for by name. Silent, because a suite that
#  printed the script's own progress output would bury its results.
info(){ :; }; ok(){ :; }; warn(){ :; }; err(){ :; }
svc(){ :; }; svc_active(){ return 1; }          # no service, so no restart path
reg(){ :; }
LLM_HOME="$TMP"; LLM_BACKEND=rocm
HW_ENV="$TMP/hardware.env"
hw_get(){ [[ -f "$HW_ENV" ]] && sed -n "s/^$1=\(.*\)$/\1/p" "$HW_ENV" | head -1; }
CONFIG="$TMP/llama-swap.yaml"; : > "$CONFIG"
API="http://127.0.0.1:9"

LCPP="$TMP/llama.cpp"; WCPP="$TMP/whisper.cpp"
mkdir -p "$LCPP" "$WCPP"
# shellcheck source=lib/update.sh
. "$REPO/lib/update.sh"

section "build directory names"

#  ROCm keeps the plain name. That asymmetry is load-bearing: renaming the
#  existing directories would invalidate every recorded rollback target on every
#  machine already running this.
check "rocm keeps the plain name"   "build-b10545" "$(bd_name b10545 rocm)"
check "vulkan is prefixed"          "build-vulkan-b10545" "$(bd_name b10545 vulkan)"
check "and the tag comes back out"  "b10545" "$(bd_tag build-b10545)"
check "from the prefixed one too"   "b10545" "$(bd_tag build-vulkan-b10545)"
check "a version tag survives it"   "v1.9.2" "$(bd_tag build-vulkan-v1.9.2)"
LLM_BACKEND=vulkan
check "the default follows the backend" "build-vulkan-b1" "$(bd_name b1)"
LLM_BACKEND=rocm
check "and back"                        "build-b1"        "$(bd_name b1)"

section "which builds belong to which backend"

#  Both backends, two versions each, plus the two things in a real llama.cpp
#  checkout that look like builds and are not.
mkdir -p "$LCPP/build-b10500" "$LCPP/build-b10545" \
         "$LCPP/build-vulkan-b10500" "$LCPP/build-vulkan-b10545"
#  Explicit timestamps. prune orders by mtime, and directories created inside the
#  same second order arbitrarily - the first version of this suite created the
#  oldest build LAST and then asserted that prune would delete it, which failed
#  for the right reason and told the wrong story.
touch -d '2026-01-01' "$LCPP/build-b10500" "$LCPP/build-vulkan-b10500"
touch -d '2026-06-01' "$LCPP/build-b10545" "$LCPP/build-vulkan-b10545"
: > "$LCPP/build-xcframework.sh"          # a FILE named build-*
mkdir -p "$LCPP/build"                    # the symlink is not there yet
rmdir "$LCPP/build"
count(){ bd_list "$LCPP" "$1" | wc -l | tr -d ' '; }
check "rocm sees only its own"    "2" "$(count rocm)"
check "vulkan sees only its own"  "2" "$(count vulkan)"
check "the shell script is not a build" "0" \
  "$(bd_list "$LCPP" rocm | grep -c xcframework)"
check "and the names are the rocm ones" "build-b10500 build-b10545" \
  "$(bd_list "$LCPP" rocm | xargs -n1 basename | sort | tr '\n' ' ' | sed 's/ $//')"

section "what the symlink says is active"

ln -sfn build-b10545 "$LCPP/build"
check "the tag"                    "b10545" "$(lcpp_active)"
check "and the backend"            "rocm"   "$(active_backend "$LCPP")"
ln -sfn build-vulkan-b10545 "$LCPP/build"
check "the same tag under vulkan"  "b10545" "$(lcpp_active)"
check "and the backend follows"    "vulkan" "$(active_backend "$LCPP")"
#  No symlink at all: an installation from before versioned builds existed.
rm -f "$LCPP/build"
check "no symlink is not vulkan"   "rocm"   "$(active_backend "$LCPP")"
ln -sfn build-b10545 "$LCPP/build"

section "prune keeps the other backend's builds"

#  KEEP_BUILDS counts per backend, so pruning under one must not touch the
#  other. Getting this wrong would delete the fallbacks you switched away from -
#  silently, and only noticed the next time you tried to switch back.
mkdir -p "$LCPP/build-b10400"                       # a third rocm build
touch -d '2025-01-01' "$LCPP/build-b10400"          # and the oldest of them
LLM_BACKEND=rocm lcpp_prune
check "the active rocm build stays"  "yes" \
  "$([[ -d "$LCPP/build-b10545" ]] && echo yes || echo no)"
check "one rocm fallback stays"      "yes" \
  "$([[ -d "$LCPP/build-b10500" ]] && echo yes || echo no)"
check "the oldest rocm build goes"   "no" \
  "$([[ -d "$LCPP/build-b10400" ]] && echo yes || echo no)"
check "both vulkan builds untouched" "2" \
  "$(find "$LCPP" -maxdepth 1 -type d -name 'build-vulkan-*' | wc -l | tr -d ' ')"

section "the backend the build was made for is not the backend that is wanted"

#  active_backend reads the symlink and llm_backend the configuration, and the
#  gap between them is exactly the window between switching and rebuilding. Code
#  that used one where it meant the other is how a machine ends up running a HIP
#  binary while every path around it says Vulkan.
printf 'LLM_BACKEND=vulkan\n' > "$HW_ENV"
unset LLM_BACKEND
check "the file decides when nothing is exported" "vulkan" "$(llm_backend)"
check "while the symlink still says rocm"         "rocm"   "$(active_backend "$LCPP")"
check "an env var still wins over the file"       "rocm"   "$(LLM_BACKEND=rocm llm_backend)"
rm -f "$HW_ENV"
LLM_BACKEND=rocm

section "the version cache"

CACHE_FILE="$UPD_CACHE"
cache_set llama b10545
cache_set swap v250
check "a value comes back"        "b10545" "$(upd_get llama)"
check "and the other one too"     "v250"   "$(upd_get swap)"
check "an unknown key is empty"   ""       "$(upd_get nonsense)"
cache_set llama b10600
check "a rewrite replaces"        "b10600" "$(upd_get llama)"
check "without losing the rest"   "v250"   "$(upd_get swap)"
rm -f "$CACHE_FILE"

section "the dirty guard"

#  Refusing to build over uncommitted work, without refusing to build over the
#  files the build itself regenerates. Both halves were bugs: counting untracked
#  files blocked on the tool's own 'build' symlink, and then whisper.cpp's CMake
#  writes a TRACKED file on every configure.
G="$TMP/repo"; mkdir -p "$G"
git -C "$G" init -q .
printf 'a\n' > "$G/tracked.txt"; printf 'b\n' > "$G/generated.json"
git -C "$G" add -A >/dev/null
git -C "$G" -c user.email=t@t -c user.name=t commit -qm init
check "a clean repo is clean"            "no" "$(repo_dirty "$G" && echo yes || echo no)"
printf 'x\n' > "$G/untracked.txt"
check "an untracked file is not dirty"   "no" "$(repo_dirty "$G" && echo yes || echo no)"
printf 'changed\n' > "$G/tracked.txt"
check "a tracked change IS dirty"        "yes" "$(repo_dirty "$G" && echo yes || echo no)"
git -C "$G" checkout -q -- tracked.txt
printf 'changed\n' > "$G/generated.json"
check "a tracked change is dirty..."     "yes" "$(repo_dirty "$G" && echo yes || echo no)"
check "...unless it is named as generated" "no" \
  "$(repo_dirty "$G" generated.json && echo yes || echo no)"

summary
