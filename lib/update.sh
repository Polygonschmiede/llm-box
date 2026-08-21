# shellcheck shell=bash
# ============================================================================
#  Updating and rolling back the engines  —  sourced by bin/llm
# ============================================================================
#  Lifted out of bin/llm, which was 1575 lines. This half shares nothing with
#  the model registry except the output helpers (info/ok/warn/err), svc/
#  svc_active, hip_flags, need_uv, hw_get and $LLM_HOME - so it is a seam, not
#  a split down the middle.
#
#  Sourced rather than executed: every function here reaches those helpers by
#  name, and re-exporting them into a child process would mean maintaining the
#  list. `llm status` needs the version helpers too, so there is no path that
#  can skip it.
#
#  Covers llama.cpp, whisper.cpp, llama-swap, Open WebUI and ComfyUI.

UPD_CACHE="$LLM_HOME/.update-cache"        # cached version query (once a day)
UPD_MAXAGE=86400
KEEP_BUILDS=2                              # active build + this many fallbacks
GH_LCPP="https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
GH_SWAP="https://api.github.com/repos/mostlygeek/llama-swap/releases/latest"
GH_WCPP="https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest"
GH_COMFY="https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest"
PYPI_OWUI="https://pypi.org/pypi/open-webui/json"
UPD_STATE="$LLM_HOME/.update-state"
# ============================================================================
#  Which backend the engines are built for
# ============================================================================
#  ROCm compiles for the exact ISA of the cards it can see; Vulkan compiles
#  SPIR-V and needs to know nothing about them. That asymmetry is the whole
#  reason Vulkan is the easier install - and the reason a Vulkan build cannot be
#  wrong about hardware it has not met.
#
#  The choice lives in config/hardware.env as LLM_BACKEND, written by
#  'llm gpu sync'; LLM_BACKEND in the environment overrides it for one command.
llm_backend(){                              # -> rocm | vulkan
  local b="${LLM_BACKEND:-$(hw_get LLM_BACKEND)}"
  case "$b" in
    rocm|vulkan) printf '%s' "$b";;
    #  Nothing recorded yet (a fresh clone, or an installation from before
    #  backends existed). Ask the library, which detects the same way.
    *) reg backend 2>/dev/null || printf 'rocm';;
  esac
}

backend_flags(){                            # -> array in BACKEND_FLAGS
  BACKEND_FLAGS=()
  case "$(llm_backend)" in
    vulkan) vulkan_flags;;
    *)      hip_flags;;
  esac
}

hip_flags(){                                # -> array in HIP_FLAGS and BACKEND_FLAGS
  local gfx="${LLM_GFX_TARGETS:-$(hw_get LLM_GFX_TARGETS)}"
  local cc="${LLM_HIP_COMPILER:-$(hw_get LLM_HIP_COMPILER)}"
  if [[ -z "$gfx" || -z "$cc" ]]; then      # 'llm gpu sync' has not run yet
    gfx="${gfx:-$(reg hw 2>/dev/null | sed -n 's/.*"gfxTargets": *"\([^"]*\)".*/\1/p')}"
    cc="${cc:-$(reg hw 2>/dev/null | sed -n 's/.*"hipCompiler": *"\([^"]*\)".*/\1/p')}"
  fi
  [[ -z "$gfx" ]] && { err "No gfx target detected. Does rocm-smi work? Otherwise: LLM_GFX_TARGETS=gfx1201 llm update llama, or switch backend: llm gpu backend vulkan"; return 1; }
  [[ -z "$cc"  ]] && { err "No HIP compiler found. Is hipcc/ROCm installed? Otherwise set LLM_HIP_COMPILER=..., or switch backend: llm gpu backend vulkan"; return 1; }
  HIP_FLAGS=(-DGGML_HIP=ON "-DAMDGPU_TARGETS=$gfx" "-DCMAKE_HIP_COMPILER=$cc")
  BACKEND_FLAGS=("${HIP_FLAGS[@]}")
}

vulkan_flags(){                             # -> array in BACKEND_FLAGS
  #  One cmake flag, and nothing card-specific. What it does need is three build
  #  dependencies, and a missing one surfaces as a cmake error a hundred lines
  #  into a log - so they are checked here, the way the build checks them.
  #
  #  Determined by building it: glslc alone is not enough, libvulkan-dev alone is
  #  not enough, and spirv-headers is needed for the SPIRV-HeadersConfig.cmake
  #  that ggml-vulkan's find_package looks for.
  local miss=""
  #  RUN it, do not just find it. A glslc that is on PATH but cannot start - a
  #  half-installed package, a prefix without its libshaderc on the library path -
  #  is worse than an absent one: ggml probes each shader extension by looking for
  #  "extension not supported" in glslc's stderr and treats anything else as
  #  SUPPORTED, so a glslc that only ever prints "error while loading shared
  #  libraries" makes every extension look available and the build then dies
  #  several thousand shader lines later. Observed exactly that way.
  glslc --version >/dev/null 2>&1 || miss="$miss glslc"
  #  Compile AND LINK, rather than looking for a header at a path. Two earlier
  #  versions of this were wrong in different ways: testing for
  #  /usr/include/vulkan/vulkan.h fails on a hand-installed SDK, and
  #  preprocessing alone passes while find_package(Vulkan) still fails - it wants
  #  Vulkan_LIBRARY too, and libvulkan.so (the dev symlink) is in a different
  #  package from the versioned runtime .so. Linking asks the build's question.
  printf '#include <vulkan/vulkan.h>\nint main(void){return (int)VK_HEADER_VERSION;}\n' \
    | c++ -x c++ - -lvulkan -o /dev/null >/dev/null 2>&1 \
    || miss="$miss libvulkan-dev"
  printf '#include <spirv/unified1/spirv.h>\n' | c++ -E -x c++ - >/dev/null 2>&1 \
    || miss="$miss spirv-headers"
  if [[ -n "$miss" ]]; then
    err "the Vulkan build needs:$miss"
    echo "  sudo apt-get install glslc libvulkan-dev spirv-headers" >&2
    echo "  glslc compiles the shaders, libvulkan-dev has the headers and the" >&2
    echo "  link-time library, spirv-headers the cmake config ggml looks for." >&2
    echo "  To RUN a model you additionally need a driver (mesa-vulkan-drivers on" >&2
    echo "  AMD and Intel) and vulkan-tools, which is where vulkaninfo comes from." >&2
    return 1
  fi
  BACKEND_FLAGS=(-DGGML_VULKAN=ON)
}

# ============================================================================
#  One build directory per version - and per backend
# ============================================================================
#  A HIP and a Vulkan build of the same tag are two different binaries, so they
#  need two directories. The backend goes in the NAME rather than into a marker
#  file inside it, which is what the first attempt did: with the name carrying it,
#  both can exist at once and switching backend is a symlink change - the same
#  seconds a rollback takes - instead of a rebuild every time.
#
#      build-b10545              ROCm  (unchanged, so every existing
#                                       installation keeps working untouched)
#      build-vulkan-b10545       Vulkan
#
#  Only non-default backends are prefixed. That asymmetry is deliberate: renaming
#  the existing directories would invalidate every recorded rollback target on
#  every machine that already runs this.
#
#  KEEP_BUILDS therefore counts PER BACKEND, and 'llm versions' lists the active
#  backend's builds. The first build of each backend still costs a build; after
#  that, back and forth is free.
bd_name(){ # $1=tag  [$2=backend, default: active]  -> directory name
  local b="${2:-$(llm_backend)}"
  if [[ "$b" == rocm ]]; then printf 'build-%s' "$1"; else printf 'build-%s-%s' "$b" "$1"; fi
}

bd_tag(){ # $1=directory name -> the tag inside it
  local n="${1#build-}"
  printf '%s' "${n#vulkan-}"
}

#  Directories of ONE backend, newest first. Scoping this is what keeps
#  lcpp_prune from deleting the other backend's fallbacks and 'llm versions' from
#  offering a rollback that would silently change backend.
bd_list(){ # $1=repository  [$2=backend, default: active]
  local b="${2:-$(llm_backend)}" glob
  glob="$(bd_name '*' "$b")"
  find "$1" -maxdepth 1 -type d -name "$glob" -printf '%T@ %p\n' 2>/dev/null \
    | { if [[ "$b" == rocm ]]; then grep -v '/build-vulkan-'; else cat; fi; } \
    | sort -rn | cut -d' ' -f2-
}

#  Which backend does the CURRENTLY ACTIVE build belong to? Read from the
#  symlink, so it is the truth about the running binary rather than about the
#  configured preference - those differ exactly between a backend switch and the
#  rebuild or re-link that follows it.
active_backend(){ # $1=repository
  local t
  [[ -L "$1/build" ]] || { printf 'rocm'; return; }
  t="$(basename "$(readlink -f "$1/build")")"
  case "$t" in build-vulkan-*) printf 'vulkan';; *) printf 'rocm';; esac
}
LCPP_CMAKE_FLAGS=(
  -DCMAKE_BUILD_TYPE=Release
  -DGGML_NATIVE=ON
  -DGGML_SCHED_MAX_COPIES=4
  -DLLAMA_CURL=ON
  -DLLAMA_BUILD_TESTS=OFF
  -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON
)
# ============================================================================
#  The idea: every llama.cpp version gets its OWN build directory
#  (build-bXXXXX/), and 'build' is only a symlink to it. Switching and rolling
#  back is therefore a symlink change - seconds, no rebuild.
#  Before switching, a smoke test runs with a real model.

lcpp_active(){                                  # before the migration: git HEAD
  if [[ -L "$LCPP/build" ]]; then bd_tag "$(basename "$(readlink -f "$LCPP/build")")"
  elif [[ -d "$LCPP/build" ]]; then git -C "$LCPP" rev-parse --short HEAD 2>/dev/null
  fi
}
# directories only, newest first (the repo also contains e.g. build-xcframework.sh)
lcpp_builds(){ bd_list "$LCPP"; }
swap_active(){ "$SWAP_BIN" --version 2>/dev/null | sed -n 's/^version: *\(v*[0-9][^ ]*\).*/\1/p'; }

# The latest upstream versions (cached; upd_refresh asks again right now)
#  Set one key in the cache without losing the others. This used to be a single
#  printf: if one source failed, the whole cache came out empty.
cache_set(){ # $1=key $2=value (empty = do nothing)
  [[ -z "${2:-}" ]] && return 0
  touch "$UPD_CACHE"
  local tmp="$UPD_CACHE.tmp"
  { grep -v "^$1=" "$UPD_CACHE" 2>/dev/null; printf '%s=%s\n' "$1" "$2"; } > "$tmp" \
    && mv "$tmp" "$UPD_CACHE"
}
#  '-L' matters: a moved GitHub repo answers 301, and without -L the query
#  silently returns an empty value, so 'llm update' claims "up to date".
#  The ComfyUI repository does exactly this today.
gh_tag(){ curl -sfL --max-time 10 "$1" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1; }
pypi_version(){ curl -sfL --max-time 10 "$1" | python3 -c '
import json,sys
try: print(json.load(sys.stdin)["info"]["version"])
except Exception: pass' 2>/dev/null; }

upd_refresh(){
  local d; d=$(mktemp -d)
  ( gh_tag "$GH_LCPP"  > "$d/llama"   ) &
  ( gh_tag "$GH_SWAP"  > "$d/swap"    ) &
  ( gh_tag "$GH_WCPP"  > "$d/whisper" ) &
  ( gh_tag "$GH_COMFY" > "$d/comfy"   ) &
  ( pypi_version "$PYPI_OWUI" > "$d/ui" ) &
  wait
  local k
  for k in llama swap whisper comfy ui; do
    cache_set "$k" "$(tr -d '\n' < "$d/$k" 2>/dev/null)"
  done
  rm -rf "$d"
  #  Which commit does each of those tags point at? See upd_same.
  upd_cache_sha llama   "$LCPP"
  upd_cache_sha whisper "$WCPP"
  upd_cache_sha comfy   "$COMFY"
}
upd_stale(){
  [[ -f "$UPD_CACHE" ]] || return 0
  [[ $(( $(date +%s) - $(stat -c %Y "$UPD_CACHE") )) -gt $UPD_MAXAGE ]]
}
upd_get(){ sed -n "s/^$1=//p" "$UPD_CACHE" 2>/dev/null; }   # llama|swap|whisper|ui|comfy
st_get(){ sed -n "s/^$1=//p" "$UPD_STATE" 2>/dev/null; }
st_set(){ # $1=key $2=value
  touch "$UPD_STATE"
  local tmp="$UPD_STATE.tmp"
  { grep -v "^$1=" "$UPD_STATE" 2>/dev/null; printf '%s=%s\n' "$1" "$2"; } > "$tmp" \
    && mv "$tmp" "$UPD_STATE"
}

# --- Is the repository clean enough to check out another tag? --------------
#  Only real changes to TRACKED files may block an update. Untracked files are
#  normal in these repositories: lcpp_switch/wcpp_switch create 'build' as a
#  symlink, and an upstream .gitignore pattern with a trailing slash (build/,
#  build-*/) does not cover a symlink - so whisper.cpp reported '?? build', its
#  own build symlink, and refused every update. If an untracked file really is
#  in the way, 'git checkout' says so itself, with the path.
#
#  Trailing arguments are paths a BUILD writes back into the source tree, which
#  are equally not the user's doing. See WCPP_GENERATED.
repo_dirty(){ # $1=repository  $2...=paths regenerated by the build
  local repo="$1"; shift
  local -a spec=(.) p
  for p in "$@"; do spec+=(":!$p"); done
  [[ -n "$(git -C "$repo" status --porcelain -uno -- "${spec[@]}" 2>/dev/null)" ]]
}
#  whisper.cpp's own top-level CMakeLists.txt runs configure_file() into
#  bindings/javascript/package.json on every configure, so one build leaves the
#  repository dirty in a TRACKED file and every later update would be refused -
#  the same dead end as the build symlink, one layer down. They are dropped
#  before the checkout instead of being carried along.
WCPP_GENERATED=(bindings/javascript/package.json)

#  After a rollback the source follows the build directory, as a courtesy: what
#  actually runs is the build. So a checkout that cannot happen must NOT become
#  the exit status of 'llm rollback' - it did, and a rollback that had worked
#  reported itself as failed, which the control page then showed in red.
rb_source(){ # $1=repository  $2=ref  $3...=paths regenerated by the build
  local repo="$1" ref="$2"; shift 2
  [[ $# -gt 0 ]] && git -C "$repo" checkout --quiet -- "$@" 2>/dev/null
  if git -C "$repo" checkout --quiet "$ref" 2>/dev/null; then
    info "source checked out at $ref as well"
  else
    warn "the build is active; the source could not follow to $ref - 'git status' in that repository says why."
  fi
  return 0
}

# --- Is 'latest' really newer, or only spelled differently? ----------------
#  Upstream sometimes ships one commit under two names. whisper.cpp does: bot
#  releases bNNNN next to hand-cut v1.x.y, and GitHub's releases/latest answers
#  "newest non-prerelease", which lands on bNNNN while v1.9.3 is still flagged
#  as a prerelease. Comparing the NAMES then reports an update forever.
#  ls-remote, not fetch: the status table must not touch a working tree.
tag_sha(){ # $1=repository  $2=tag  ->  commit sha, or nothing
  [[ -z "${2:-}" || ! -d "$1/.git" ]] && return 0
  #  The peeled entry (...^{}) is the commit an annotated tag points at; a
  #  lightweight tag has none, so fall back to the ref itself.
  git -C "$1" ls-remote --tags origin "refs/tags/$2^{}" "refs/tags/$2" 2>/dev/null \
    | awk '/\^\{\}$/{print $1; f=1; exit} {l=$1} END{if(!f) print l}'
}
local_sha(){ # $1=repository  $2=tag  ->  sha, empty when this clone lacks the tag
  [[ -z "${2:-}" ]] && return 0
  git -C "$1" rev-parse --verify --quiet "$2^{commit}" 2>/dev/null
}
#  Stored as "<tag> <sha>" so the entry invalidates itself the moment upstream
#  publishes a different tag - a bare sha would outlive the tag it belongs to.
upd_cache_sha(){ # $1=cache key  $2=repository
  local tag sha
  tag=$(upd_get "$1"); [[ -z "$tag" ]] && return 0
  sha=$(tag_sha "$2" "$tag"); [[ -z "$sha" ]] && return 0
  cache_set "${1}_sha" "$tag $sha"
}
upd_get_sha(){ # $1=cache key  ->  sha, but only if it belongs to the cached tag
  local tag rec
  tag=$(upd_get "$1"); rec=$(upd_get "${1}_sha")
  [[ -n "$tag" && -n "$rec" && "${rec% *}" == "$tag" ]] && printf '%s' "${rec#* }"
}
#  Already there = the same name, or a different name for the same commit. When
#  either sha is unknown this says no and the caller keeps the name comparison:
#  offering an update too often is the harmless direction.
upd_is_target(){ # $1=cache key  $2=repository  $3=active  $4=target tag
  local sha act
  [[ -z "${3:-}" || -z "${4:-}" ]] && return 1
  [[ "${3#v}" == "${4#v}" ]] && return 0
  #  The cached sha speaks for the cached tag only, so an explicitly requested
  #  version is resolved on the spot - an update is a network operation anyway.
  if [[ "$4" == "$(upd_get "$1")" ]]; then sha=$(upd_get_sha "$1")
  else sha=$(tag_sha "$2" "$4"); fi
  [[ -z "$sha" ]] && return 1
  act=$(local_sha "$2" "$3"); [[ -n "$act" && "$act" == "$sha" ]]
}
#  The same question against whatever upstream currently calls the latest - what
#  'llm update' and 'llm status' print, without touching the network.
upd_same(){ # $1=cache key  $2=repository  $3=active version
  upd_is_target "$1" "$2" "$3" "$(upd_get "$1")"
}

# --- whisper.cpp: speech to text, its own project next to llama.cpp --------
#  Built the same way as llama.cpp: build-<tag>/ plus a symlink build/, so a
#  rollback is only a symlink change. Serves /v1/audio/transcriptions.
wcpp_active(){
  if [[ -L "$WCPP/build" ]]; then bd_tag "$(basename "$(readlink -f "$WCPP/build")")"
  elif [[ -d "$WCPP/build" ]]; then git -C "$WCPP" rev-parse --short HEAD 2>/dev/null
  fi
}
wcpp_builds(){ bd_list "$WCPP"; }

# Tests a whisper build BEFORE it becomes active by transcribing the bundled
# sample. If that fails, the old build stays active.
wcpp_smoke(){ # $1=build directory
  local bd="$1" model out
  [[ -x "$bd/bin/whisper-server" ]] || { err "no whisper-server in $bd/bin"; return 1; }
  [[ -f "$WCPP/samples/jfk.wav" ]] || { warn "no sample to test with - skipped."; return 0; }
  model=$(grep -oE '\-m +[^ "]+\.bin' "$CONFIG" 2>/dev/null | awk '{print $2}' | head -1)
  [[ -f "$model" ]] || { warn "no whisper model in the configuration - smoke test skipped."; return 0; }
  info "smoke-testing whisper with $(basename "$model") ..."
  out=$(LD_LIBRARY_PATH="$bd/bin" "$bd/bin/whisper-cli" -m "$model" -f "$WCPP/samples/jfk.wav" -nt 2>/dev/null)
  if [[ -z "${out// /}" ]]; then err "whisper-cli returned no text."; return 1; fi
  ok "smoke test passed: ${out:0:60}..."
}

wcpp_switch(){ # $1=target version
  local tgt prev prev_dir
  tgt="$(bd_name "$1")"; prev=$(wcpp_active)
  prev_dir="$([[ -L "$WCPP/build" ]] && basename "$(readlink -f "$WCPP/build")")"
  [[ -d "$WCPP/$tgt" ]] || { err "build directory $tgt is missing."; return 1; }
  local was_active=no; svc_active && was_active=yes
  [[ "$was_active" == yes ]] && { info "stopping llama-swap ..."; svc stop; }
  ln -sfn "$tgt" "$WCPP/build" || { err "could not set the symlink."; return 1; }
  if [[ "$was_active" == yes ]]; then
    info "starting llama-swap ..."; svc start; sleep 2
    #  The same guard as lcpp_switch: whisper is served through llama-swap, so a
    #  build that takes the endpoint down must not stay active.
    if ! curl -sf --max-time 10 "$API/v1/models" >/dev/null; then
      err "the API stopped answering - rolling back to ${prev:-the previous build}."
      [[ -n "$prev_dir" ]] && ln -sfn "$prev_dir" "$WCPP/build"
      svc restart; return 1
    fi
  fi
  ok "active: whisper.cpp $1  (was: ${prev:-?})"
}

# Clean up old builds, exactly as lcpp_prune does - whisper builds are the same
# size and used to accumulate without limit.
wcpp_prune(){
  local act d n keep=$KEEP_BUILDS
  act=$(wcpp_active)
  for d in $(wcpp_builds); do
    n=$(basename "$d"); n="${n#build-}"
    [[ "$n" == "$act" ]] && continue
    if [[ $keep -gt 1 ]]; then keep=$((keep-1)); continue; fi
    info "removing the old build: $n ($(du -sh "$d" 2>/dev/null | cut -f1))"; rm -rf "$d"
  done
}

update_whisper(){ # $1=target tag (empty = the latest release)
  command -v cmake >/dev/null || { err "cmake is missing."; return 1; }
  if [[ ! -d "$WCPP/.git" ]]; then
    info "whisper.cpp is not installed - fetching it into $WCPP ..."
    git clone --quiet https://github.com/ggml-org/whisper.cpp.git "$WCPP" || { err "git clone failed."; return 1; }
  fi
  local tgt="${1:-}" act free
  if [[ -z "$tgt" ]]; then upd_refresh; tgt=$(upd_get whisper); fi
  [[ -z "$tgt" ]] && { err "could not determine the latest version (network?)."; return 1; }
  act=$(wcpp_active)
  if upd_is_target whisper "$WCPP" "$act" "$tgt"; then
    if [[ "$act" == "$tgt" ]]; then ok "whisper.cpp is up to date ($act)."
    else ok "whisper.cpp is up to date ($act is the same commit as $tgt)."; fi
    return 0
  fi
  if repo_dirty "$WCPP" "${WCPP_GENERATED[@]}"; then
    err "the whisper.cpp repository has changes to tracked files - please clean up first."; return 1
  fi
  #  llama.cpp checks this too, and a whisper build is not much smaller.
  free=$(df --output=avail -BG "$WCPP" | tail -1 | tr -dc 0-9)
  [[ ${free:-0} -lt 8 ]] && { err "not enough disk space (${free}G free, ~8G needed)."; return 1; }
  #  Before the fetch, for the same reason as in update_llama.
  backend_flags || return 1
  local dir; dir="$WCPP/$(bd_name "$tgt")"
  if [[ -d "$dir" ]]; then
    info "build $tgt for $(llm_backend) already exists - testing it, then switching."
    wcpp_smoke "$dir" || { err "smoke test failed - $tgt will NOT be activated."; return 1; }
    wcpp_switch "$tgt" && wcpp_prune; return $?
  fi
  info "fetching whisper.cpp $tgt ..."
  # A shallow clone (--depth 1) does not have the objects of other tags, so a
  # later 'checkout' would fail. Fetch the full history once.
  if [[ "$(git -C "$WCPP" rev-parse --is-shallow-repository 2>/dev/null)" == "true" ]]; then
    info "repository is a shallow clone - fetching the full history ..."
    git -C "$WCPP" fetch --unshallow --quiet origin || { err "git fetch --unshallow failed."; return 1; }
  fi
  git -C "$WCPP" fetch --tags --quiet origin || { err "git fetch failed."; return 1; }
  #  Throw away what the last build generated - the guard above already decided
  #  that nothing else in the tree is modified, so this can only drop our own
  #  residue, and leaving it would make the checkout fail.
  git -C "$WCPP" checkout --quiet -- "${WCPP_GENERATED[@]}" 2>/dev/null
  git -C "$WCPP" checkout --quiet "$tgt" || { err "tag '$tgt' not found."; return 1; }
  local log="$LLM_HOME/.build-whisper-$(bd_name "$tgt").log" t0=$SECONDS
  info "building whisper.cpp $tgt for $(llm_backend) with $(nproc) threads (log: $log) ..."
  # The same backend flags as llama.cpp - only the project-specific test option
  # differs. whisper.cpp is ggml too, so -DGGML_VULKAN=ON works there unchanged.
  if ! cmake -S "$WCPP" -B "$dir" -DCMAKE_BUILD_TYPE=Release "${BACKEND_FLAGS[@]}" \
        -DGGML_NATIVE=ON -DWHISPER_BUILD_TESTS=OFF -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON > "$log" 2>&1; then
    err "cmake configuration failed:"; tail -8 "$log" >&2; rm -rf "$dir"; return 1
  fi
  if ! cmake --build "$dir" -j "$(nproc)" >> "$log" 2>&1; then
    err "build failed:"; grep -iE "error" "$log" | tail -8 >&2
    warn "the active build (${act:-none}) is untouched - nothing is broken."
    rm -rf "$dir"; return 1
  fi
  ok "build finished in $(( (SECONDS-t0)/60 )) min $(( (SECONDS-t0)%60 ))s."
  wcpp_smoke "$dir" || { err "smoke test failed - $tgt will NOT be activated."; return 1; }
  wcpp_switch "$tgt" && wcpp_prune
}

# One-time migration: the existing real build/ becomes build-<sha>/
lcpp_migrate(){
  [[ -L "$LCPP/build" ]] && return 0
  [[ -d "$LCPP/build" ]] || return 0
  local cur; cur=$(git -C "$LCPP" rev-parse --short HEAD 2>/dev/null || echo unknown)
  info "one-time migration to versioned builds: build/ -> build-$cur/"
  mv "$LCPP/build" "$LCPP/build-$cur" && ln -sfn "build-$cur" "$LCPP/build" || {
    err "migration failed."; return 1; }
  ok "the previous build stays as a fallback: $cur"
}

# Test a new build BEFORE it becomes active: load the smallest available model,
# wait for /health, force a tiny answer. LD_LIBRARY_PATH matters because older
# builds carry an absolute RUNPATH to .../build/bin.
smoke_test(){ # $1=build directory
  local bd="$1" model port=5987 log="$LLM_HOME/.smoke.log" pid code out
  [[ -x "$bd/bin/llama-server" ]] || { err "no llama-server in $bd/bin"; return 1; }
  # Take a model from the configuration (an entry's '-m') and pick the smallest.
  # Not simply the smallest GGUF in the directory: that would be the MTP drafter,
  # which cannot be loaded on its own.
  model=$(grep -oE '\-m +[^ "]+\.gguf' "$CONFIG" 2>/dev/null | awk '{print $2}' | sort -u \
          | while read -r f; do [[ -f "$f" ]] && printf "%s %s\n" "$(stat -c %s "$f")" "$f"; done \
          | sort -n | head -1 | cut -d' ' -f2-)
  if [[ -z "$model" ]]; then warn "no loadable model in the configuration - smoke test skipped."; return 0; fi
  info "smoke test with $(basename "$model") ..."
  #  The device flag and the mask are spelled per backend: '--device ROCm0' with
  #  HIP_VISIBLE_DEVICES, '--device Vulkan0' with GGML_VK_VISIBLE_DEVICES. Card 0
  #  is the LOGICAL first compute card either way, which is what the mask makes
  #  it - the point of pinning here is that the test does not depend on whichever
  #  card happens to be free.
  local dev mask_var mask
  case "$(llm_backend)" in
    vulkan) dev=Vulkan0; mask_var=GGML_VK_VISIBLE_DEVICES;;
    *)      dev=ROCm0;   mask_var=HIP_VISIBLE_DEVICES;;
  esac
  mask="$(hw_get "$mask_var")"
  #  env, not a bare "$mask_var=$mask": bash only treats a LITERAL name=value
  #  word as an assignment, so an expanded one would be looked up as the command.
  LD_LIBRARY_PATH="$bd/bin" env "$mask_var=$mask" \
    "$bd/bin/llama-server" \
    -m "$model" -c 512 -ngl 99 -fa on --device "$dev" -sm none -mg 0 \
    --host 127.0.0.1 --port "$port" --no-webui > "$log" 2>&1 &
  pid=$!
  code=""
  for _ in $(seq 1 180); do
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
      err "the test server died. Last lines ($log):"; tail -6 "$log" >&2; return 1
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health" 2>/dev/null)
    [[ "$code" == 200 ]] && break
  done
  if [[ "$code" != 200 ]]; then
    err "the test server never became ready (6 min). Last lines ($log):"; tail -6 "$log" >&2
    kill "$pid" 2>/dev/null; return 1
  fi
  out=$(curl -s --max-time 120 "http://127.0.0.1:$port/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d '{"max_tokens":16,"messages":[{"role":"user","content":"Reply with just: ok"}]}' 2>/dev/null)
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  if ! grep -q '"content"' <<<"$out"; then
    err "the test server returned no answer."; return 1
  fi
  ok "smoke test passed (load + answer)."
}

# Move the symlink, restart the service, check that the API answers again
lcpp_switch(){ # $1=target version
  #  prev_dir, not "build-$prev": the way back has to be the directory that was
  #  actually linked, which under the other backend has a different name.
  local tgt prev prev_dir
  tgt="$(bd_name "$1")"; prev=$(lcpp_active)
  prev_dir="$([[ -L "$LCPP/build" ]] && basename "$(readlink -f "$LCPP/build")")"
  [[ -d "$LCPP/$tgt" ]] || { err "build directory $tgt is missing."; return 1; }
  local was_active=no; svc_active && was_active=yes
  [[ "$was_active" == yes ]] && { info "stopping llama-swap ..."; svc stop; }
  ln -sfn "$tgt" "$LCPP/build" || { err "could not set the symlink."; return 1; }
  if [[ "$was_active" == yes ]]; then
    info "starting llama-swap ..."; svc start; sleep 2
    if ! curl -sf --max-time 10 "$API/v1/models" >/dev/null; then
      err "the API stopped answering - rolling back to $prev."
      [[ -n "$prev_dir" ]] && ln -sfn "$prev_dir" "$LCPP/build"
      svc restart; return 1
    fi
  fi
  ok "active: llama.cpp $1 ($(llm_backend))  (was: ${prev:-?})"
}

# Clean up old builds: the active one plus KEEP_BUILDS-1 fallbacks stay
lcpp_prune(){
  local act d n keep=$KEEP_BUILDS
  act=$(lcpp_active)
  for d in $(lcpp_builds); do
    n=$(basename "$d"); n="${n#build-}"
    [[ "$n" == "$act" ]] && continue
    if [[ $keep -gt 1 ]]; then keep=$((keep-1)); continue; fi
    info "removing the old build: $n ($(du -sh "$d" 2>/dev/null | cut -f1))"; rm -rf "$d"
  done
}

#  After a backend switch: move the symlinks onto this backend's builds wherever
#  they already exist. This is what makes "back and forth as you like" true - the
#  first build of each backend costs a build, everything after that is a relink,
#  the same seconds a rollback takes.
#
#  Only the exact active tag is considered. Silently relinking to some older
#  version that happens to be built for the new backend would change two things
#  at once, and "which version am I running" is the question this project answers
#  most often.
backend_relink(){
  local want pending="" act
  want="$(llm_backend)"
  act=$(lcpp_active)
  if [[ "$(active_backend "$LCPP")" != "$want" ]]; then
    if [[ -n "$act" && -d "$LCPP/$(bd_name "$act" "$want")" ]]; then
      info "llama.cpp $act is already built for $want - relinking."
      lcpp_switch "$act" || return 1
    else
      pending="llm update llama"
    fi
  fi
  act=$(wcpp_active)
  if [[ -d "$WCPP/.git" && "$(active_backend "$WCPP")" != "$want" ]]; then
    if [[ -n "$act" && -d "$WCPP/$(bd_name "$act" "$want")" ]]; then
      info "whisper.cpp $act is already built for $want - relinking."
      wcpp_switch "$act" || return 1
    else
      pending="${pending:+$pending, }llm update whisper"
    fi
  fi
  if [[ -n "$pending" ]]; then
    warn "no $want build yet for the active version:  $pending"
    warn "the endpoint keeps running on the current binaries until then."
  fi
  return 0
}

update_llama(){ # $1=target tag (empty = the latest release)
  command -v cmake >/dev/null || { err "cmake is missing.  sudo apt-get install cmake build-essential"; return 1; }
  #  On a first run llama.cpp is not there at all - fetch it instead of failing.
  #  That makes 'llm update llama' the setup command too: clone, build, smoke
  #  test, switch. (The same pattern as update_whisper.)
  if [[ ! -d "$LCPP/.git" ]]; then
    if [[ -e "$LCPP" && ! -L "$LCPP" ]]; then
      err "$LCPP exists but is not a git repository - please move it aside."; return 1
    fi
    info "llama.cpp is not here - fetching it into $LCPP ..."
    git clone --quiet https://github.com/ggml-org/llama.cpp.git "$LCPP" || {
      err "git clone failed."; return 1; }
  fi
  lcpp_migrate || return 1
  local tgt="${1:-}" act free
  if [[ -z "$tgt" ]]; then upd_refresh; tgt=$(upd_get llama); fi
  [[ -z "$tgt" ]] && { err "could not determine the latest version (network?)."; return 1; }
  act=$(lcpp_active)
  if upd_is_target llama "$LCPP" "$act" "$tgt"; then
    if [[ "$act" == "$tgt" ]]; then ok "llama.cpp is up to date ($act)."
    else ok "llama.cpp is up to date ($act is the same commit as $tgt)."; fi
    return 0
  fi
  free=$(df --output=avail -BG "$LCPP" | tail -1 | tr -dc 0-9)
  [[ ${free:-0} -lt 8 ]] && { err "not enough disk space (${free}G free, ~8G needed)."; return 1; }
  #  Before anything is fetched or checked out. This used to sit at the cmake
  #  call, so a machine missing glslc had already moved the source tree to the
  #  new tag by the time it was told - the active build was safe (that is what
  #  the symlink is for) but the checkout was left on a version nothing was
  #  built from.
  backend_flags || return 1
  if repo_dirty "$LCPP"; then
    err "the llama.cpp repository has changes to tracked files - please clean up first."; return 1
  fi
  # Already built? Test it anyway - the directory may be left over from a
  # failed attempt.
  #  A build of this tag FOR THIS BACKEND: relink and be done. This is what makes
  #  switching back and forth a symlink change once each side has been built.
  local dir; dir="$LCPP/$(bd_name "$tgt")"
  if [[ -d "$dir" ]]; then
    info "build $tgt for $(llm_backend) already exists - testing it, then switching."
    smoke_test "$dir" || { err "smoke test failed - $tgt will NOT be activated."; return 1; }
    lcpp_switch "$tgt" && lcpp_prune; return $?
  fi

  info "fetching llama.cpp $tgt ..."
  git -C "$LCPP" fetch --tags --quiet origin || { err "git fetch failed."; return 1; }
  git -C "$LCPP" checkout --quiet "$tgt" || { err "tag '$tgt' not found."; return 1; }

  #  The log carries the backend too, so two builds of one tag do not overwrite
  #  each other's log the way they used to share a directory.
  local log="$LLM_HOME/.build-$(bd_name "$tgt").log" t0=$SECONDS
  info "building $tgt for $(llm_backend) with $(nproc) threads (takes a while; log: $log) ..."
  if ! cmake -S "$LCPP" -B "$dir" "${LCPP_CMAKE_FLAGS[@]}" "${BACKEND_FLAGS[@]}" \
       > "$log" 2>&1; then
    err "cmake configuration failed:"; tail -8 "$log" >&2; rm -rf "$dir"; return 1
  fi
  if ! cmake --build "$dir" -j "$(nproc)" >> "$log" 2>&1; then
    err "build failed:"; grep -iE "error" "$log" | tail -8 >&2
    warn "the active build ($act) is untouched - nothing is broken."
    rm -rf "$dir"; return 1
  fi
  ok "build finished in $(( (SECONDS-t0)/60 )) min $(( (SECONDS-t0)%60 ))s."

  if ! smoke_test "$dir"; then
    err "smoke test failed - $tgt will NOT be activated."
    warn "$act stays active. The build directory is kept: $dir"
    return 1
  fi
  lcpp_switch "$tgt" || return 1
  lcpp_prune
}

update_swap(){ # $1=target version (empty = the latest)
  local cur want url tmp
  cur=$(swap_active)
  want="${1:-}"
  if [[ -z "$want" ]]; then upd_refresh; want=$(upd_get swap); fi
  [[ -z "$want" ]] && { err "could not determine the latest llama-swap version."; return 1; }
  [[ "$cur" == "$want" ]] && { ok "llama-swap is up to date ($cur)."; return 0; }
  #  Ask for the release we actually want. This used to read releases/latest
  #  whatever version was requested, so 'llm update swap v240' downloaded the
  #  newest tarball and then reported itself as v240.
  local rel meta
  rel="$GH_SWAP"
  [[ -n "${1:-}" ]] && rel="${GH_SWAP%/latest}/tags/$want"
  tmp=$(mktemp -d)
  if ! curl -sf --max-time 15 "$rel" -o "$tmp/release.json"; then
    err "could not read the release information for $want."; rm -rf "$tmp"; return 1
  fi
  meta="$tmp/release.json"
  url=$(sed -n 's/.*"browser_download_url": *"\([^"]*linux_amd64[^"]*\)".*/\1/p' "$meta" | head -1)
  [[ -z "$url" ]] && { err "no linux_amd64 download in release $want."; rm -rf "$tmp"; return 1; }
  #  This binary is the one thing this project installs that it does not build
  #  from source. Until now the only gate was '--version', which a replaced
  #  tarball would pass. The release ships a checksums file; refuse rather than
  #  install something unverified.
  local sums arc
  sums=$(sed -n 's/.*"browser_download_url": *"\([^"]*checksums[^"]*\)".*/\1/p' "$meta" | head -1)
  [[ -z "$sums" ]] && { err "release $want ships no checksums file - refusing to install it unverified."; rm -rf "$tmp"; return 1; }
  arc="${url##*/}"                                  # the name the checksums list uses
  info "downloading llama-swap $want ..."
  if ! curl -sfL --max-time 120 "$url" -o "$tmp/$arc"; then err "the download failed."; rm -rf "$tmp"; return 1; fi
  if ! curl -sfL --max-time 30 "$sums" -o "$tmp/checksums.txt"; then err "the checksums file could not be downloaded."; rm -rf "$tmp"; return 1; fi
  if ! ( cd "$tmp" && grep -F " $arc" checksums.txt | sha256sum -c - >/dev/null 2>&1 ); then
    err "the checksum of $arc does not match the release list - NOT installing it."
    echo "  expected: $(grep -F " $arc" "$tmp/checksums.txt" | awk '{print $1}')" >&2
    echo "  got:      $(sha256sum "$tmp/$arc" | awk '{print $1}')" >&2
    rm -rf "$tmp"; return 1
  fi
  ok "checksum verified against the release list."
  tar -xzf "$tmp/$arc" -C "$tmp" || { err "the archive is broken."; rm -rf "$tmp"; return 1; }
  local new; new=$(find "$tmp" -type f -name 'llama-swap' | head -1)
  [[ -z "$new" ]] && { err "no llama-swap inside the archive."; rm -rf "$tmp"; return 1; }
  chmod +x "$new"
  if ! "$new" --version >/dev/null 2>&1; then err "the new binary does not run."; rm -rf "$tmp"; return 1; fi
  cp -f "$SWAP_BIN" "$LLM_HOME/bin/llama-swap-$cur" 2>/dev/null && info "old version kept: llama-swap-$cur"
  install -m 755 "$new" "$SWAP_BIN"; rm -rf "$tmp"
  # Check the configuration with the new binary, then restart the service
  svc_active && { svc restart; sleep 2; }
  if svc_active && ! curl -sf --max-time 10 "$API/v1/models" >/dev/null; then
    err "llama-swap $want does not answer - rolling back."
    install -m 755 "$LLM_HOME/bin/llama-swap-$cur" "$SWAP_BIN"; svc restart; return 1
  fi
  ok "llama-swap $want is active (was $cur)."
}

# ============================================================================
#  Keeping Open WebUI (the chat UI) and ComfyUI current
# ============================================================================
#  The same pattern as llama.cpp: determine the version, save a way back,
#  update, smoke test, roll back automatically on failure.
#  The difference: instead of a directory per version we save two small things:
#    * 'uv pip freeze'  = the COMPLETE dependency closure
#    * webui.db         = the database (only ~1 MB)
#  The database is the reason. Open WebUI runs alembic migrations at startup and
#  those only go forward, so downgrading the code against an already-migrated
#  database is not safe without this snapshot. Disk space is NOT the reason: uv
#  hardlinks from ~/.cache/uv, so a second venv shares most of its bytes with the
#  first. That also means 'du -sh venv-webui' (~6.6 GB here) is the logical size,
#  not what deleting it would give back.

owui_active(){
  [[ -x "$VENV_UI/bin/python" ]] || return 0
  "$VENV_UI/bin/python" -c 'import importlib.metadata as m; print(m.version("open-webui"))' 2>/dev/null
}
owui_smoke(){   # waits until the UI answers
  for _ in $(seq 1 40); do
    curl -sf -m 3 http://127.0.0.1:3000/health >/dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}
update_owui(){ # $1=target version (empty = the latest)
  need_uv || return 1
  [[ -x "$VENV_UI/bin/python" ]] || { err "no venv for the chat UI - run:  llm setup"; return 1; }
  local tgt="${1:-}" act
  if [[ -z "$tgt" ]]; then upd_refresh; tgt=$(upd_get ui); fi
  [[ -z "$tgt" ]] && { err "could not determine the latest version (network?)."; return 1; }
  act=$(owui_active)
  [[ "$act" == "$tgt" ]] && { ok "Open WebUI is up to date ($act)."; return 0; }
  info "Open WebUI $act -> $tgt"

  # Save the way back BEFORE touching anything
  local frz="$LLM_HOME/.venv-freeze-$act.txt" bak="$OWUI_DATA.bak-$act"
  "$UV" pip freeze --python "$VENV_UI/bin/python" > "$frz" 2>/dev/null || {
    err "could not save the current state - aborting."; rm -f "$frz"; return 1; }
  if [[ -d "$OWUI_DATA" ]]; then
    rm -rf "$bak"; mkdir -p "$bak"
    cp -a "$OWUI_DATA"/webui.db* "$bak"/ 2>/dev/null || true
  fi
  st_set owui_prev "$act"
  info "way back saved: $(basename "$frz") + $(basename "$bak")"

  if ! "$UV" pip install --python "$VENV_UI/bin/python" "open-webui==$tgt" 2>&1 | tail -3; then
    err "installation failed - restoring the previous state."
    "$UV" pip install --quiet --python "$VENV_UI/bin/python" -r "$frz" >/dev/null 2>&1
    return 1
  fi
  local was_active=no; systemctl --user is-active open-webui >/dev/null 2>&1 && was_active=yes
  if [[ "$was_active" == yes ]]; then
    systemctl --user restart open-webui
    if ! owui_smoke; then
      err "the chat UI does not answer after the update - rolling back."
      rollback_owui "$act"; return 1
    fi
    # The registry runs in its OWN venv - check it anyway.
    curl -sf -m 5 http://127.0.0.1:8081/api/health >/dev/null 2>&1 \
      || warn "the registry does not answer - please check: llm api status"
  fi
  ok "Open WebUI is now $tgt.  (back: llm rollback ui)"
  # Clean up old snapshots (small, but not unlimited)
  ls -1t "$LLM_HOME"/.venv-freeze-*.txt 2>/dev/null | tail -n +$((KEEP_BUILDS + 1)) | xargs -r rm -f
  ls -1dt "$OWUI_DATA".bak-* 2>/dev/null | tail -n +$((KEEP_BUILDS + 1)) | xargs -r rm -rf
}
rollback_owui(){ # $1=target version
  local v="${1:-$(st_get owui_prev)}"
  [[ -z "$v" ]] && { err "no saved version (llm versions)."; return 1; }
  local frz="$LLM_HOME/.venv-freeze-$v.txt" bak="$OWUI_DATA.bak-$v"
  [[ -f "$frz" ]] || { err "no snapshot for $v: $frz"; return 1; }
  info "rolling Open WebUI back to $v ..."
  "$UV" pip install --quiet --python "$VENV_UI/bin/python" -r "$frz" || return 1
  #  The database MUST come back too: the migrations only go forward.
  if [[ -d "$bak" ]]; then
    systemctl --user stop open-webui 2>/dev/null || true
    cp -a "$bak"/webui.db* "$OWUI_DATA"/ 2>/dev/null || true
    info "database restored from $(basename "$bak")"
  else
    warn "no database snapshot - the chat history may cause trouble."
  fi
  systemctl --user is-enabled open-webui >/dev/null 2>&1 && systemctl --user start open-webui
  ok "Open WebUI is back at $v."
}

# --- ComfyUI: git checkout plus dependencies --------------------------------
comfy_active(){
  [[ -d "$COMFY/.git" ]] || return 0
  git -C "$COMFY" describe --tags --exact-match HEAD 2>/dev/null \
    || sed -n 's/^__version__ = "\(.*\)"/\1/p' "$COMFY/comfyui_version.py" 2>/dev/null
}
#  A shallow clone has no tags - the same trap as with whisper.cpp.
git_prepare_tags(){ # $1=directory
  if [[ "$(git -C "$1" rev-parse --is-shallow-repository 2>/dev/null)" == "true" ]]; then
    info "repository is a shallow clone - fetching the full history ..."
    git -C "$1" fetch --unshallow --quiet origin || return 1
  fi
  git -C "$1" fetch --tags --quiet origin
}
comfy_smoke(){
  "$COMFY/venv/bin/python" -c 'import torch
assert torch.cuda.is_available(), "no GPU visible"
assert torch.version.hip, "torch without ROCm"' 2>&1 | tail -2
}
#  ComfyUI is the one part of this stack with no Vulkan path at all: it runs on
#  PyTorch, PyTorch's GPU support here is the ROCm wheel index, and there is no
#  Vulkan build of PyTorch to point at. Saying so here is better than failing
#  somewhere inside a 3 GB wheel download.
comfy_backend_ok(){
  [[ "$(llm_backend)" == vulkan ]] || return 0
  err "ComfyUI needs ROCm and the backend is set to vulkan."
  echo "  PyTorch has no Vulkan build, so image generation is the one thing here" >&2
  echo "  that Vulkan cannot do. Options:" >&2
  echo "    * install ROCm and switch:  llm gpu backend rocm" >&2
  echo "    * or leave ComfyUI out - everything else works under Vulkan" >&2
  echo "  See docs/COMFYUI.md." >&2
  return 1
}

update_comfy(){ # $1=target tag
  [[ -d "$COMFY/.git" ]] || { err "ComfyUI is not installed ($COMFY). First: bash bin/install-comfyui.sh"; return 1; }
  local tgt="${1:-}" act
  if [[ -z "$tgt" ]]; then upd_refresh; tgt=$(upd_get comfy); fi
  [[ -z "$tgt" ]] && { err "could not determine the latest version (network?)."; return 1; }
  act=$(comfy_active)
  if upd_is_target comfy "$COMFY" "$act" "$tgt"; then ok "ComfyUI is up to date ($act)."; return 0; fi
  #  Your own custom_nodes/, models/, output/ are normal here and must not block
  #  the update - see repo_dirty.
  if repo_dirty "$COMFY"; then
    err "the ComfyUI repository has changes to tracked files - please clean up first."; return 1
  fi
  git_prepare_tags "$COMFY" || { err "git fetch failed."; return 1; }
  local prev; prev=$(git -C "$COMFY" rev-parse --short HEAD)
  st_set comfy_prev "$prev"
  info "ComfyUI $act -> $tgt  (back would be: $prev)"
  local was_active=no; systemctl --user is-active comfyui >/dev/null 2>&1 && was_active=yes
  [[ "$was_active" == yes ]] && systemctl --user stop comfyui
  if ! git -C "$COMFY" checkout --quiet "$tgt"; then
    err "tag '$tgt' not found."; [[ "$was_active" == yes ]] && systemctl --user start comfyui; return 1
  fi
  #  Do NOT rebuild the dependencies here - the installer knows the ROCm index.
  bash "$LLM_HOME/bin/install-comfyui.sh" --deps-only || {
    err "dependencies failed - rolling back."
    git -C "$COMFY" checkout --quiet "$prev"
    bash "$LLM_HOME/bin/install-comfyui.sh" --deps-only >/dev/null 2>&1
    [[ "$was_active" == yes ]] && systemctl --user start comfyui
    return 1; }
  if ! comfy_smoke; then
    err "torch no longer sees the GPU - rolling back."
    rollback_comfy "$prev"; return 1
  fi
  #  If the service was off it stays off: ComfyUI holds VRAM while it runs.
  [[ "$was_active" == yes ]] && systemctl --user start comfyui
  ok "ComfyUI is now $tgt.  (back: llm rollback comfy)"
  warn "your custom_nodes may expect a newer ComfyUI API - worth a look."
}
rollback_comfy(){ # $1=target ref
  local v="${1:-$(st_get comfy_prev)}"
  [[ -z "$v" ]] && { err "no saved version (llm versions)."; return 1; }
  info "rolling ComfyUI back to $v ..."
  systemctl --user stop comfyui 2>/dev/null || true
  git -C "$COMFY" checkout --quiet "$v" || return 1
  bash "$LLM_HOME/bin/install-comfyui.sh" --deps-only >/dev/null 2>&1
  ok "ComfyUI is back at $v."
}
