#!/usr/bin/env bash
# ============================================================================
#  Check the VRAM arithmetic: KV cache size and the per-card fit
# ============================================================================
#  Why: these two functions decide whether a load runs out of memory. Getting
#  kv_cache_bytes() wrong by a factor - which it was, multiplying the whole
#  cache by the slot count - silently refuses configurations that fit, or
#  accepts ones that do not. And check_fit()'s own docstring records that it
#  used to SUM the free VRAM across cards and therefore reported "fits" for a
#  model that needs its share on every one of them.
#
#  kv_cache_bytes() takes an injected header, so the three layouts are checked
#  against synthetic metadata instead of committing gigabytes of GGUF.
#
#  Run with:  bash tests/vram-matrix.sh      (or: bash tests/run-all.sh)
# ============================================================================
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

GB=1073741824

# ---------------------------------------------------------------------------
#  Three headers, one per layout llama.cpp actually uses.
#    classic  - every layer keeps KV over the whole context
#    hybrid   - Qwen3.x: only every Nth layer has KV, the rest hold SSM state
#    swa      - Gemma 4: windowed layers store the window, not the context
# ---------------------------------------------------------------------------
read -r -d '' HEADERS <<'PYEOF'
CLASSIC = {
    "general.architecture": "llama",
    "llama.block_count": 32,
    "llama.attention.head_count_kv": 8,
    "llama.attention.key_length": 128,
    "llama.attention.value_length": 128,
}
HYBRID = dict(CLASSIC, **{
    "general.architecture": "qwen35",
    "qwen35.block_count": 32,
    "qwen35.attention.head_count_kv": 8,
    "qwen35.attention.key_length": 128,
    "qwen35.attention.value_length": 128,
    "qwen35.full_attention_interval": 4,          # every 4th layer only
    "qwen35.ssm.inner_size": 4096,
    "qwen35.ssm.conv_kernel": 4,
    "qwen35.ssm.state_size": 128,
})
SWA = {
    "general.architecture": "gemma4",
    "gemma4.block_count": 4,
    "gemma4.attention.head_count_kv": 4,
    "gemma4.attention.key_length": 256,
    "gemma4.attention.value_length": 256,
    "gemma4.attention.key_length_swa": 128,
    "gemma4.attention.value_length_swa": 128,
    "gemma4.attention.sliding_window": 1024,
    "gemma4.attention.sliding_window_pattern": [1, 1, 1, 0],   # 3 windowed, 1 full
}
kv = lambda m, c, q="f16", n=1: llmreg.kv_cache_bytes("", c, q, n, meta=m)
PYEOF

#  LLM_HOME, even though these read no file. kv_cache_bytes(meta=...) computes
#  from the dict it is handed, so today this passes either way - but without it
#  llmreg binds to the real checkout, and the next check added to this section
#  that DOES touch the configuration would silently read the live machine. That
#  is the class of leak tests/lib.sh documents as having already burned this
#  project once.
v(){ LLM_HOME="$PROBE_HOME" pyx "$HEADERS
$1"; }

section "classic layout: linear in context, and in the quant"
#  32 layers * 8 kv heads * (128+128) * ctx * bytes-per-element
check "8k, f16"   "1073741824" "$(v 'print(kv(CLASSIC, 8192))')"
check "16k, f16"  "2147483648" "$(v 'print(kv(CLASSIC, 16384))')"
check "8k, q8_0"  "570425344"  "$(v 'print(kv(CLASSIC, 8192, "q8_0"))')"
check "q8_0 is 17/32 of f16" "True" \
  "$(v 'print(kv(CLASSIC, 8192, "q8_0") == int(kv(CLASSIC, 8192) * 1.0625 / 2))')"
check "unknown quant falls back to f16" "True" \
  "$(v 'print(kv(CLASSIC, 8192, "nonsense") == kv(CLASSIC, 8192))')"

section "slots do not multiply the attention cache"
#  -c is the total either way: llama.cpp shares it across sequences with -kvu or
#  divides it without. It allocates n_ctx cells in both cases.
check "1 slot vs 4 slots" "True" \
  "$(v 'print(kv(CLASSIC, 8192, "f16", 1) == kv(CLASSIC, 8192, "f16", 4))')"
check "16 slots change nothing" "True" \
  "$(v 'print(kv(CLASSIC, 8192, "f16", 1) == kv(CLASSIC, 8192, "f16", 16))')"

section "hybrid: only every 4th layer holds KV"
check "a quarter of the classic cache, plus SSM state" "True" \
  "$(v 'print(kv(HYBRID, 8192) > kv(CLASSIC, 8192) / 4)')"
check "clearly below the classic cache" "True" \
  "$(v 'print(kv(HYBRID, 8192) < kv(CLASSIC, 8192) / 3)')"
#  The recurrent state IS per sequence - the one part that scales with slots.
check "SSM state scales with the slots" "True" \
  "$(v 'print(kv(HYBRID, 8192, "f16", 4) > kv(HYBRID, 8192, "f16", 1))')"
check "and the difference is exactly 3 states" "True" \
  "$(v '
one, four = kv(HYBRID, 8192, "f16", 1), kv(HYBRID, 8192, "f16", 4)
state = 24 * (4 * 4096 + 128 * 4096) * 4          # 24 SSM layers, f32
print(four - one == 3 * state)')"

section "sliding window: windowed layers cap at the window"
check "below a same-size classic model" "True" \
  "$(v 'print(kv(SWA, 32768) < kv(dict(SWA, **{"gemma4.attention.sliding_window": 0}), 32768))')"
check "grows only through the full layer" "True" \
  "$(v '
a, b = kv(SWA, 8192), kv(SWA, 16384)
full = 4 * 512 * 8192 * 2                          # the one non-windowed layer
print(b - a == full)')"

section "guards"
check "no context -> no answer" "None" "$(v 'print(kv(CLASSIC, 0))')"
check "empty header -> no answer" "None" "$(v 'print(kv({}, 8192))')"
check "header without layers -> no answer" "None" \
  "$(v 'print(kv({"general.architecture": "x"}, 8192))')"

# ---------------------------------------------------------------------------
#  check_fit needs the card list, which the rocm-smi fixtures already fake.
#  The fixtures report ~34 GB total with ~0.17/0.07 GB in use.
# ---------------------------------------------------------------------------
section "check_fit counts per card, not the sum"
fit(){ # $1=fixture  $2=weights GB  $3=card number or "both"  $4=field
  local gpu mode
  if [[ "$3" == both ]]; then gpu="'both'"; mode=both; else gpu="$3"; mode=single; fi
  LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$FIXTURES/rocm-smi-$1.sh" \
  LLM_DGPUS='' LLM_MIN_VRAM_GB='' pyx "
m = {'runtime': {'gpu': {'device': $gpu, 'mode': '$mode'},
                 'contextWindow': None, 'kvCacheQuant': None, 'parallel': 1},
     'vram': {'weightsBytes': int($2 * $GB)},
     'files': {}, 'state': 'unloaded'}
print(llmreg.check_fit(m, gpu=$gpu)['$4'])"
}
check "20 GB on one 34 GB card"        "True"  "$(fit 2card 20 0 ok)"
check "40 GB on one 34 GB card"        "False" "$(fit 2card 40 0 ok)"
#  The regression the docstring records: 40 GB over two cards is 20 GB each and
#  fits; summing the free space would also have called 60 GB a fit.
check "40 GB over two cards"           "True"  "$(fit 2card 40 both ok)"
check "60 GB over two cards"           "False" "$(fit 2card 60 both ok)"
check "60 GB over three cards"         "True"  "$(fit 3card 60 both ok)"
check "the 8% headroom is applied"     "True" \
  "$(LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" pyx "
m = {'runtime': {'gpu': {'device': 0, 'mode': 'single'}, 'contextWindow': None,
                 'kvCacheQuant': None, 'parallel': 1},
     'vram': {'weightsBytes': 10 * $GB}, 'files': {}, 'state': 'unloaded'}
print(llmreg.check_fit(m, gpu=0)['needBytes'] == int(10 * $GB * 1.08))")"
check "no cards detected -> no refusal" "True" "$(fit none 999 0 ok)"
check "a loaded model does not block itself" "True" \
  "$(LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" pyx "
m = {'runtime': {'gpu': {'device': 0, 'mode': 'single'}, 'contextWindow': None,
                 'kvCacheQuant': None, 'parallel': 1},
     'vram': {'weightsBytes': 30 * $GB}, 'files': {}, 'state': 'ready'}
print(llmreg.check_fit(m, gpu=0)['ok'])")"
check "the reason names the tight card" "True" \
  "$(fit 2card 40 0 reason | grep -qi 'card 0' && echo True || echo False)"

summary
