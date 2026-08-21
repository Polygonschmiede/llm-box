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
  "$(LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$FIXTURES/rocm-smi-igpu-last.sh" LLM_DGPUS=1 \
     pyx "print(llmreg.hw()['hipVisibleDevices'])")"
check "igpu-last, iGPU forced"         "2" \
  "$(LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$FIXTURES/rocm-smi-igpu-last.sh" LLM_DGPUS=2 \
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
  LLM_HOME="$home" LLM_ROCM_SMI="$FIXTURES/rocm-smi-$1.sh" LLM_DGPUS='' LLM_MIN_VRAM_GB='' \
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

# ---------------------------------------------------------------------------
#  Power and utilisation come from the same single rocm-smi query as the
#  temperature. The trap is the label: a discrete card reports "Average
#  Graphics Package Power", an APU "Current Socket Graphics Package Power", so
#  matching the whole phrase would silently drop the figure on half the
#  machines this runs on.
# ---------------------------------------------------------------------------
section "Power draw and utilisation per card"
check "2card       watts on card 0"  "17.0" \
  "$(probe 2card      "llmreg.gpus()[0]['powerW']")"
check "2card       busy on card 0"   "42" \
  "$(probe 2card      "llmreg.gpus()[0]['busyPercent']")"
check "2card       busy on card 1"   "0" \
  "$(probe 2card      "llmreg.gpus()[1]['busyPercent']")"
check "apu-16gb    the socket label parses too" "18.0" \
  "$(probe apu-16gb   "llmreg.gpus()[0]['powerW']")"
check "igpu-first  the iGPU is still filtered out" "2" \
  "$(probe igpu-first "len(llmreg.gpus())")"
#  The table is what 'llm status', 'llm gpu list' and 'llm watch' all print, so
#  the figures have to survive the rendering and not just the parse.
check "the table carries both" "True" \
  "$(LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" \
     LLM_DGPUS='' LLM_MIN_VRAM_GB='' python3 "$REPO/lib/llmreg.py" gpus --table \
     | grep -q '17 W  busy  42 %' && echo True || echo False)"
#  An older ROCm that does not answer --showpower at all: the field has to come
#  out as '?' rather than as a confident 0 W.
NOPOWER="$TMP/rocm-smi-nopower.sh"
{ printf '#!/bin/sh\ncat <<SMI\n'
  grep -v 'Power\|GPU use' "$FIXTURES/rocm-smi-1card.sh" | sed -n '4,$p' | head -n -2
  printf 'SMI\n'; } > "$NOPOWER"
chmod +x "$NOPOWER"
check "a sensor the driver does not answer is '?'" "True" \
  "$(LLM_HOME="$PROBE_HOME" LLM_ROCM_SMI="$NOPOWER" \
     LLM_DGPUS='' LLM_MIN_VRAM_GB='' python3 "$REPO/lib/llmreg.py" gpus --table \
     | grep -q 'junction   29°C     ? W  busy   ? %' && echo True || echo False)"

# ============================================================================
#  The same answers from the other backend
# ============================================================================
#  This is the actual claim of the Vulkan work: one answer, two sources. So the
#  expectations below are the SAME numbers as above, produced from a fake
#  vulkaninfo plus a fake amdgpu sysfs tree instead of from fake rocm-smi output.
#  Where they legitimately differ - the ISA target, which Vulkan does not need -
#  they are asserted as different on purpose.
section "vulkan: the same card counts"

check "1card       one card"        "1" "$(vprobe 1card      "llmreg.gpu_count()")"
check "2card       two cards"       "2" "$(vprobe 2card      "llmreg.gpu_count()")"
check "3card       three cards"     "3" "$(vprobe 3card      "llmreg.gpu_count()")"
check "igpu-last   iGPU excluded"   "2" "$(vprobe igpu-last  "llmreg.gpu_count()")"
check "igpu-first  iGPU excluded"   "2" "$(vprobe igpu-first "llmreg.gpu_count()")"
check "apu-16gb    the 16 GB APU excluded" "1" "$(vprobe apu-16gb "llmreg.gpu_count()")"
check "mixed       both counted"    "2" "$(vprobe mixed      "llmreg.gpu_count()")"
check "none        nothing"         "0" "$(vprobe none       "llmreg.gpu_count()")"
#  Vulkan offers a software rasteriser on every machine with mesa. Running a
#  model on llvmpipe would be a disaster, so it is excluded by device type - and
#  so is the iGPU beside it.
check "llvmpipe    software device excluded" "1" \
  "$(vprobe llvmpipe   "llmreg.gpu_count()")"
#  ggml skips CPU-type devices, so its Vulkan0 is the first real GPU whatever
#  position the software one holds. Counting every vulkaninfo entry gives the
#  right answer only while mesa keeps llvmpipe last - so this fixture puts it
#  first, and then the absolute indices have to be 0 and 1, not 1 and 2.
check "llvmpipe-first  two real cards"      "2" \
  "$(vprobe llvmpipe-first "llmreg.gpu_count()")"
check "llvmpipe-first  logical 0 is absolute 0" "0" \
  "$(vprobe llvmpipe-first "llmreg.to_smi(0)")"
check "llvmpipe-first  logical 1 is absolute 1" "1" \
  "$(vprobe llvmpipe-first "llmreg.to_smi(1)")"
check "llvmpipe-first  and the mask says 0,1" "0,1" \
  "$(vprobe llvmpipe-first "llmreg.hw()['hipVisibleDevices']")"

section "vulkan: absolute and logical are still two things"

check "igpu-first  logical 0 is absolute 1" "1" "$(vprobe igpu-first "llmreg.to_smi(0)")"
check "igpu-first  logical 1 is absolute 2" "2" "$(vprobe igpu-first "llmreg.to_smi(1)")"
check "igpu-first  absolute 0 is no card"   "None" "$(vprobe igpu-first "llmreg.to_logical(0)")"
check "igpu-first  the mask names 1,2"      "1,2" \
  "$(vprobe igpu-first "llmreg.hw()['hipVisibleDevices']")"
check "igpu-last   the mask names 0,1"      "0,1" \
  "$(vprobe igpu-last  "llmreg.hw()['hipVisibleDevices']")"
check "the mask is written under its own name" "GGML_VK_VISIBLE_DEVICES" \
  "$(vprobe 2card      "llmreg.hw()['visibleEnv']")"
check "and the device prefix follows"         "Vulkan" \
  "$(vprobe 2card      "llmreg.device_prefix()")"

section "vulkan: the sensors, joined by DRM minor and not by position"

#  mk-vulkan.py permutes the minors (device 0 -> card3, device 1 -> card1). If
#  this pairing were done by position, device 0 would report card1's 30.0 °C.
check "2card       junction on card 0" "29.0" \
  "$(vprobe 2card      "llmreg.gpus()[0]['tempJunctionC']")"
check "2card       junction on card 1" "30.0" \
  "$(vprobe 2card      "llmreg.gpus()[1]['tempJunctionC']")"
check "2card       watts on card 0"    "17.0" \
  "$(vprobe 2card      "llmreg.gpus()[0]['powerW']")"
check "2card       busy on card 0"     "42" \
  "$(vprobe 2card      "llmreg.gpus()[0]['busyPercent']")"
check "2card       busy on card 1"     "0" \
  "$(vprobe 2card      "llmreg.gpus()[1]['busyPercent']")"
check "2card       VRAM matches rocm-smi to the byte" \
  "$(probe 2card       "llmreg.gpus()[0]['vramTotalBytes']")" \
  "$(vprobe 2card      "llmreg.gpus()[0]['vramTotalBytes']")"
#  The APU exposes power1_input where a discrete card exposes power1_average -
#  the same split rocm-smi words as "Current Socket" versus "Average".
check "apu-16gb    the other power file parses too" "18.0" \
  "$(vprobe apu-16gb   "llmreg.gpus()[0]['powerW']")"
#  Only the sensor LABELLED junction. temp1 is 'edge' and reads 30 °C on every
#  card, so taking the first one would look plausible and be wrong.
check "the edge sensor is not mistaken for the junction" "False" \
  "$(vprobe 3card      "llmreg.gpus()[0]['tempJunctionC'] == 30.0")"
#  A second hwmon that is not amdgpu sits in the same directory in the real
#  tree, and the fixture puts a 99 °C 'junction' in it.
check "a foreign hwmon is skipped" "False" \
  "$(vprobe 2card      "llmreg.gpus()[0]['tempJunctionC'] == 99.0")"

section "vulkan: a card the driver says nothing about"

#  Identity from Vulkan, no amdgpu sysfs - an Intel or NVIDIA card. It still has
#  to be detected and counted, with the sensors simply absent.
check "no-sysfs    both cards still detected" "2" \
  "$(vprobe no-sysfs   "llmreg.gpu_count()")"
check "no-sysfs    and named"                 "True" \
  "$(vprobe no-sysfs   "'Arc' in llmreg.gpus()[0]['name']")"
check "no-sysfs    no junction temperature"   "None" \
  "$(vprobe no-sysfs   "llmreg.gpus()[0].get('tempJunctionC')")"
check "no-sysfs    no watts"                  "None" \
  "$(vprobe no-sysfs   "llmreg.gpus()[0].get('powerW')")"
#  Absent is not zero: refusing every model on such a card while claiming it has
#  "0.0 GB free" would be a confident wrong answer.
check "no-sysfs    an unknown size does not refuse a model" "True" \
  "$(vprobe no-sysfs   "llmreg.check_fit({'runtime': {'gpu': {'device': 0, 'mode': 'single'}, 'contextWindow': 4096, 'kvCacheQuant': None, 'parallel': 1, 'specDecoding': None}, 'vram': {'weightsBytes': 20*1024**3}, 'files': {'model': {'path': '/nope.gguf'}}, 'name': 'x', 'state': 'stopped'})['ok']")"
check "no-sysfs    and says why instead"      "True" \
  "$(vprobe no-sysfs   "'not checked' in llmreg.check_fit({'runtime': {'gpu': {'device': 0, 'mode': 'single'}, 'contextWindow': 4096, 'kvCacheQuant': None, 'parallel': 1, 'specDecoding': None}, 'vram': {'weightsBytes': 20*1024**3}, 'files': {'model': {'path': '/nope.gguf'}}, 'name': 'x', 'state': 'stopped'})['reason']")"
#  In the table an absent sensor is '?', and the card is still on it.
check "no-sysfs    the table shows the card with '?'" "True" \
  "$(LLM_HOME="$PROBE_HOME" LLM_BACKEND=vulkan \
     LLM_VULKANINFO="$FIXTURES/vulkaninfo-no-sysfs.sh" LLM_SYSFS_ROOT="$FIXTURES/sysfs-no-sysfs" \
     LLM_DGPUS='' LLM_MIN_VRAM_GB='' python3 "$REPO/lib/llmreg.py" gpus --table \
     | grep -q 'junction    ?°C     ? W  busy   ? %' && echo True || echo False)"

section "vulkan: what the build needs, and does not"

#  The reason this backend is the easier install: nothing card-specific to
#  compile for, so nothing that can be compiled for the wrong card.
check "no ISA target is needed"  "" "$(vprobe 2card "llmreg.gfx_targets()")"
check "and no HIP compiler"      "None" "$(vprobe 2card "llmreg.hip_compiler()")"
#  Whereas under ROCm both are required and detected.
check "under rocm the target is detected" "gfx1201" "$(probe 2card "llmreg.gfx_targets()")"
check "mixed cards give both targets" "gfx1100;gfx1201" "$(probe mixed "llmreg.gfx_targets()")"

section "the backend choice itself"

check "an explicit choice wins"        "vulkan" "$(vprobe 2card "llmreg.backend_name()")"
check "and the other way round"        "rocm"   "$(probe 2card  "llmreg.backend_name()")"
check "an unknown name is not accepted" "rocm"  \
  "$(LLM_HOME="$PROBE_HOME" LLM_BACKEND=nonsense LLM_ROCM_SMI="$FIXTURES/rocm-smi-2card.sh" \
     pyx "print(llmreg.backend_name())" 2>/dev/null)"
#  A config written for one backend keeps working on the other: gpu_of accepts
#  both device prefixes and both mask names, whichever is active.
check "a ROCm pin is still read under vulkan" "0" \
  "$(vprobe 2card "llmreg.gpu_of({'cmd': 'x --device ROCm0 -sm none', 'env': []})['device']")"
check "a Vulkan pin is still read under rocm"  "1" \
  "$(probe 2card  "llmreg.gpu_of({'cmd': 'x --device Vulkan1 -sm none', 'env': []})['device']")"
check "a HIP mask is still read under vulkan"  "1" \
  "$(vprobe 2card "llmreg.gpu_of({'cmd': 'whisper-server -m w', 'env': ['HIP_VISIBLE_DEVICES=1']})['device']")"
check "a Vulkan mask is still read under rocm" "0" \
  "$(probe 2card  "llmreg.gpu_of({'cmd': 'whisper-server -m w', 'env': ['GGML_VK_VISIBLE_DEVICES=0']})['device']")"

summary
