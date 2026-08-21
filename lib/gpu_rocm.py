# ============================================================================
#  Card detection through rocm-smi
# ============================================================================
#  Lifted out of llmreg._smi_cards() unchanged when the Vulkan backend arrived,
#  so that one function did not have to grow two personalities. The contract is
#  cards() -> {absolute index: {...}} and everything above it - the absolute vs
#  logical translation, the LLM_DGPUS override, the fit arithmetic - stays in
#  llmreg and is shared with the other backend.
#
#  Keys a card may carry. All optional except smiIndex: a sensor the driver does
#  not answer must be ABSENT rather than zero, because the table renderer prints
#  '?' for absent and a confident '0 W' for present-and-zero.
#
#      smiIndex        the absolute index, i.e. how this backend counts
#      name            product name
#      gfx             ISA target, e.g. gfx1201
#      vramTotalBytes  vramUsedBytes
#      tempJunctionC   powerW           busyPercent
#      discrete        True/False when the backend can tell; absent when it
#                      cannot, in which case llmreg falls back to the heuristics
#                      below
# ============================================================================
import glob
import os
import re
import shutil
import subprocess

NAME = "rocm"
#  What '--device <this>N' is called, and the environment variable that hides
#  cards from the runtime. Both are backend-specific spellings of the same idea.
DEVICE_PREFIX = "ROCm"
VISIBLE_ENV = "HIP_VISIBLE_DEVICES"

#  Overridable so the 1-card, 3-card and iGPU cases can be tested without the
#  matching hardware (see tests/fixtures).
SMI = os.environ.get("LLM_ROCM_SMI", "rocm-smi")

#  The iGPU (or CPU path) appears as another "GPU" in rocm-smi and carries a CPU
#  name. Filtering by VRAM alone is unreliable: rocm-smi reports 0.5 GB for that
#  device while HIP reports 31 GB of system memory for the SAME device. On an APU
#  with a large UMA carve-out a threshold therefore flips - the name is the more
#  dependable signal, the threshold only a fallback.
CPU_NAME_RE = re.compile(r"ryzen|epyc|threadripper|athlon|core processor", re.I)


def available() -> bool:
    """Is this backend usable on this machine?

    Both tools, not just one: rocm-smi without hipcc detects cards that nothing
    can then be built for, which is a worse answer than "not available".
    """
    return bool(shutil.which(SMI) or os.path.exists(SMI)) and bool(shutil.which("hipcc"))


def cards() -> dict[int, dict]:
    """EVERY device rocm-smi knows about, iGPU included. Keys are absolute.

    One query yields temperature, power draw, utilisation, VRAM, name and gfx
    target.
    """
    try:
        raw = subprocess.run([SMI, "--showtemp", "--showpower", "--showuse",
                              "--showmeminfo", "vram", "--showproductname"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        raw = ""
    out: dict[int, dict] = {}
    for line in raw.splitlines():
        m = re.match(r"GPU\[(\d+)\]\s*:\s*(.*)", line.strip())
        if not m:
            continue
        idx, rest = int(m.group(1)), m.group(2)
        val = rest.split(":")[-1].strip()
        c = out.setdefault(idx, {"smiIndex": idx})
        if "junction" in rest.lower():
            c["tempJunctionC"] = _num(val)
        #  Discrete cards report "Average Graphics Package Power", an APU
        #  "Current Socket Graphics Package Power" - match the part they share.
        elif "Graphics Package Power" in rest:
            c["powerW"] = _num(val)
        elif "GPU use (%)" in rest:
            c["busyPercent"] = _num(val)
        elif "Total Memory" in rest:
            c["vramTotalBytes"] = _num(val)
        elif "Used Memory" in rest:
            c["vramUsedBytes"] = _num(val)
        elif "Card Series" in rest:
            c["name"] = val
        elif "GFX Version" in rest:
            c["gfx"] = val
    return out


def gfx_targets(cards_by_index: dict[int, dict], indices: list[int]) -> str:
    """gfx targets for AMDGPU_TARGETS, e.g. 'gfx1201' or 'gfx1100;gfx1201'.

    Discrete cards only - building for the iGPU's gfx target would be wasted
    build time and fails outright on some ROCm versions.
    """
    seen = sorted({cards_by_index[i].get("gfx") for i in indices
                   if cards_by_index.get(i, {}).get("gfx")})
    return ";".join(seen)


def compiler() -> str | None:
    """Path to the HIP compiler for CMAKE_HIP_COMPILER."""
    try:
        p = subprocess.run(["hipconfig", "--hipclangpath"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
        if p and os.path.exists(os.path.join(p, "clang++")):
            return os.path.join(p, "clang++")
    except (OSError, subprocess.SubprocessError):
        pass
    for cand in ("/opt/rocm/llvm/bin/clang++", shutil.which("amdclang++"),
                 shutil.which("hipcc")):
        if cand and os.path.exists(cand):
            return cand
    #  Newest llvm from the distribution, otherwise nothing.
    found = sorted(glob.glob("/usr/lib/llvm-*/bin/clang++"))
    return found[-1] if found else None


def missing_hint() -> str:
    """What to install when available() says no."""
    return ("ROCm is not complete: rocm-smi and hipcc both have to be on PATH.\n"
            "  sudo apt-get install rocm-smi rocminfo hipcc rocm-device-libs\n"
            "  or follow https://rocm.docs.amd.com/ - then 'llm gpu backend rocm'")


#  The same eight lines as llmreg._num, deliberately. Importing it would make
#  this module depend on the one that imports it, and the alternative - a shared
#  helpers module for one coercion - is not worth a third file. Keep them
#  identical: the rocm-smi values are bare numbers by the time they get here, and
#  a regex variant would silently accept "17 W" where this one returns None.
def _num(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
