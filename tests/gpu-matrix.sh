#!/usr/bin/env bash
# ============================================================================
#  Check the card detection against fake rocm-smi output
# ============================================================================
#  Why: the difference between the ABSOLUTE number (rocm-smi,
#  HIP_VISIBLE_DEVICES) and the LOGICAL one (--device ROCmN) went unnoticed for a
#  long time, because the discrete cards happen to sit at 0,1. On a machine where
#  the iGPU sorts first, mixing them up silently addresses the wrong card. The
#  'igpu-first' case is therefore the most important test in this file.
#
#  Run with:  bash tests/gpu-matrix.sh      (or: bash tests/run-all.sh)
# ============================================================================
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

section "Card count"
check 1card        1  "$(probe 1card      'llmreg.gpu_count()')"
check 2card        2  "$(probe 2card      'llmreg.gpu_count()')"
check 3card        3  "$(probe 3card      'llmreg.gpu_count()')"
check igpu-last    2  "$(probe igpu-last  'llmreg.gpu_count()')"
check igpu-first   2  "$(probe igpu-first 'llmreg.gpu_count()')"
check apu-16gb     1  "$(probe apu-16gb   'llmreg.gpu_count()')"
check none         0  "$(probe none       'llmreg.gpu_count()')"

section "HIP_VISIBLE_DEVICES (absolute numbers)"
check 1card        "0"      "$(probe 1card      "llmreg.hw()['hipVisibleDevices']")"
check 3card        "0,1,2"  "$(probe 3card      "llmreg.hw()['hipVisibleDevices']")"
check igpu-last    "0,1"    "$(probe igpu-last  "llmreg.hw()['hipVisibleDevices']")"
check igpu-first   "1,2"    "$(probe igpu-first "llmreg.hw()['hipVisibleDevices']")"
check apu-16gb     "1"      "$(probe apu-16gb   "llmreg.hw()['hipVisibleDevices']")"

section "Translation logical <-> absolute"
check "igpu-first  to_smi(0)"     1     "$(probe igpu-first 'llmreg.to_smi(0)')"
check "igpu-first  to_smi(1)"     2     "$(probe igpu-first 'llmreg.to_smi(1)')"
check "igpu-first  to_smi(2)"     None  "$(probe igpu-first 'llmreg.to_smi(2)')"
check "igpu-first  to_logical(0)" None  "$(probe igpu-first 'llmreg.to_logical(0)')"
check "igpu-first  to_logical(2)" 1     "$(probe igpu-first 'llmreg.to_logical(2)')"
check "igpu-last   to_logical(2)" None  "$(probe igpu-last  'llmreg.to_logical(2)')"
check "2card       to_smi(1)"     1     "$(probe 2card      'llmreg.to_smi(1)')"

section "tensor_split: absent on one card, even otherwise"
check 1card      None    "$(probe 1card     'llmreg.tensor_split()')"
check 2card      "1,1"   "$(probe 2card     'llmreg.tensor_split()')"
check 3card      "1,1,1" "$(probe 3card     'llmreg.tensor_split()')"
check igpu-first "1,1"   "$(probe igpu-first 'llmreg.tensor_split()')"
check apu-16gb   None    "$(probe apu-16gb  'llmreg.tensor_split()')"

section "gfx targets: compute cards only, several combined"
check 3card      "gfx1201"           "$(probe 3card      'llmreg.gfx_targets()')"
check igpu-first "gfx1201"           "$(probe igpu-first 'llmreg.gfx_targets()')"
check apu-16gb   "gfx1201"           "$(probe apu-16gb   'llmreg.gfx_targets()')"
check mixed      "gfx1100;gfx1201"   "$(probe mixed      'llmreg.gfx_targets()')"

section "Warnings"
check "none      reports the missing card"  1 "$(probe none  "len(llmreg.hw()['warnings'])")"
check "mixed     reports unequal size"      1 "$(probe mixed "len(llmreg.hw()['warnings'])")"
check "2card     stays quiet"               0 "$(probe 2card "len(llmreg.hw()['warnings'])")"

section "LLM_DGPUS overrides the detection"
check "igpu-last, only card 1 allowed" "1" \
  "$(LLM_ROCM_SMI="$FIXTURES/rocm-smi-igpu-last.sh" LLM_DGPUS=1 \
     pyx "print(llmreg.hw()['hipVisibleDevices'])")"
check "igpu-last, iGPU forced"         "2" \
  "$(LLM_ROCM_SMI="$FIXTURES/rocm-smi-igpu-last.sh" LLM_DGPUS=2 \
     pyx "print(llmreg.hw()['hipVisibleDevices'])")"

# ---------------------------------------------------------------------------
#  The write path. gpu_of() reads HIP_VISIBLE_DEVICES back through to_logical(),
#  so _patch_model has to translate the other way with to_smi() before writing
#  it. It did not, which on an iGPU-first machine pointed whisper at the wrong
#  card - the exact confusion this file exists to catch, one function further on.
# ---------------------------------------------------------------------------
section "whisper card: written absolute, read logical"
whisper_roundtrip(){ # $1=fixture  $2=logical card -> what lands in the env
  local home; home="$(sandbox)"
  add_block "$home" whisper \
    "/bin/whisper-server -m /w.bin --host 127.0.0.1 --port \${PORT} --request-path /v1/audio --inference-path /transcriptions" \
    '    env:
      - "HIP_VISIBLE_DEVICES=0"'
  #  Writes for real and reads the value back OUT OF THE FILE. Asserting on the
  #  returned note instead would pass even when the wrong number is written.
  LLM_HOME="$home" LLM_ROCM_SMI="$FIXTURES/rocm-smi-$1.sh" LLM_DGPUS= LLM_MIN_VRAM_GB= \
  LLM_SWAP_API="http://127.0.0.1:9" \
    pyx "
import re
llmreg.patch_model('whisper', {'gpu': $2, 'force': True})
print(re.search(r'HIP_VISIBLE_DEVICES=(\d+)', llmreg.config_text()).group(1))"
}
check "igpu-first  logical 0 -> absolute 1" 1 "$(whisper_roundtrip igpu-first 0)"
check "igpu-first  logical 1 -> absolute 2" 2 "$(whisper_roundtrip igpu-first 1)"
check "igpu-last   logical 1 -> absolute 1" 1 "$(whisper_roundtrip igpu-last 1)"
check "2card       logical 1 -> absolute 1" 1 "$(whisper_roundtrip 2card 1)"

summary
