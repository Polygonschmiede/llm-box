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
#  Run with:  bash tests/gpu-matrix.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

python3 tests/fixtures/mk-smi.py >/dev/null || { echo "fixtures could not be generated"; exit 1; }

fails=0
check(){ # $1=case  $2=expected  $3=actual
  if [[ "$2" == "$3" ]]; then
    printf '  \033[0;32mok\033[0m    %-46s %s\n' "$1" "$3"
  else
    printf '  \033[0;31mFAIL\033[0m  %-46s expected %-18s got %s\n' "$1" "$2" "$3"
    fails=$((fails + 1))
  fi
}

probe(){ # $1=fixture  $2=expression -> one line
  LLM_ROCM_SMI="./tests/fixtures/rocm-smi-$1.sh" LLM_DGPUS= LLM_MIN_VRAM_GB= \
    python3 -c "
import sys; sys.path.insert(0, 'lib'); import llmreg
print($2)" 2>&1 | tail -1
}

echo "Card count"
check 1card        1  "$(probe 1card      'llmreg.gpu_count()')"
check 2card        2  "$(probe 2card      'llmreg.gpu_count()')"
check 3card        3  "$(probe 3card      'llmreg.gpu_count()')"
check igpu-last    2  "$(probe igpu-last  'llmreg.gpu_count()')"
check igpu-first   2  "$(probe igpu-first 'llmreg.gpu_count()')"
check apu-16gb     1  "$(probe apu-16gb   'llmreg.gpu_count()')"
check none         0  "$(probe none       'llmreg.gpu_count()')"

echo
echo "HIP_VISIBLE_DEVICES (absolute numbers)"
check 1card        "0"      "$(probe 1card      "llmreg.hw()['hipVisibleDevices']")"
check 3card        "0,1,2"  "$(probe 3card      "llmreg.hw()['hipVisibleDevices']")"
check igpu-last    "0,1"    "$(probe igpu-last  "llmreg.hw()['hipVisibleDevices']")"
check igpu-first   "1,2"    "$(probe igpu-first "llmreg.hw()['hipVisibleDevices']")"
check apu-16gb     "1"      "$(probe apu-16gb   "llmreg.hw()['hipVisibleDevices']")"

echo
echo "Translation logical <-> absolute"
check "igpu-first  to_smi(0)"     1     "$(probe igpu-first 'llmreg.to_smi(0)')"
check "igpu-first  to_smi(1)"     2     "$(probe igpu-first 'llmreg.to_smi(1)')"
check "igpu-first  to_smi(2)"     None  "$(probe igpu-first 'llmreg.to_smi(2)')"
check "igpu-first  to_logical(0)" None  "$(probe igpu-first 'llmreg.to_logical(0)')"
check "igpu-first  to_logical(2)" 1     "$(probe igpu-first 'llmreg.to_logical(2)')"
check "igpu-last   to_logical(2)" None  "$(probe igpu-last  'llmreg.to_logical(2)')"
check "2card       to_smi(1)"     1     "$(probe 2card      'llmreg.to_smi(1)')"

echo
echo "tensor_split: absent on one card, even otherwise"
check 1card      None    "$(probe 1card     'llmreg.tensor_split()')"
check 2card      "1,1"   "$(probe 2card     'llmreg.tensor_split()')"
check 3card      "1,1,1" "$(probe 3card     'llmreg.tensor_split()')"
check igpu-first "1,1"   "$(probe igpu-first 'llmreg.tensor_split()')"
check apu-16gb   None    "$(probe apu-16gb  'llmreg.tensor_split()')"

echo
echo "gfx targets: compute cards only, several combined"
check 3card      "gfx1201"           "$(probe 3card      'llmreg.gfx_targets()')"
check igpu-first "gfx1201"           "$(probe igpu-first 'llmreg.gfx_targets()')"
check apu-16gb   "gfx1201"           "$(probe apu-16gb   'llmreg.gfx_targets()')"
check mixed      "gfx1100;gfx1201"   "$(probe mixed      'llmreg.gfx_targets()')"

echo
echo "Warnings"
check "none      reports the missing card"  1 "$(probe none  "len(llmreg.hw()['warnings'])")"
check "mixed     reports unequal size" 1 "$(probe mixed "len(llmreg.hw()['warnings'])")"
check "2card     stays quiet"               0 "$(probe 2card "len(llmreg.hw()['warnings'])")"

echo
echo "LLM_DGPUS overrides the detection"
check "igpu-last, only card 1 allowed" "1" \
  "$(LLM_ROCM_SMI=./tests/fixtures/rocm-smi-igpu-last.sh LLM_DGPUS=1 \
     python3 -c "import sys;sys.path.insert(0,'lib');import llmreg;print(llmreg.hw()['hipVisibleDevices'])" 2>&1 | tail -1)"
check "igpu-last, iGPU forced"      "2" \
  "$(LLM_ROCM_SMI=./tests/fixtures/rocm-smi-igpu-last.sh LLM_DGPUS=2 \
     python3 -c "import sys;sys.path.insert(0,'lib');import llmreg;print(llmreg.hw()['hipVisibleDevices'])" 2>&1 | tail -1)"

echo
if [[ $fails -eq 0 ]]; then
  printf '\033[0;32mAll checks passed.\033[0m\n'
else
  printf '\033[0;31m%d check(s) failed.\033[0m\n' "$fails"
fi
exit $((fails > 0))
