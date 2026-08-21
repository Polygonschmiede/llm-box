# ============================================================================
#  Card detection through Vulkan, with the telemetry from amdgpu's sysfs
# ============================================================================
#  Same contract as gpu_rocm: cards() -> {absolute index: {...}}, with the keys
#  documented there. Everything above it is shared.
#
#  Two sources, because Vulkan alone cannot answer the question:
#
#  1. IDENTITY AND ORDER come from `vulkaninfo`. Its device order is the loader's
#     physical-device order, which is the same order ggml enumerates, so GPU<n>
#     here is the n in '--device VulkanN' - before any GGML_VK_VISIBLE_DEVICES
#     mask, exactly as rocm-smi's index is the one HIP_VISIBLE_DEVICES wants.
#     The full text output rather than --summary, because only the full one
#     carries VkPhysicalDeviceDrmPropertiesEXT.primaryMinor, and that is the
#     exact DRM card number. Both cost about 50 ms; --summary would force a
#     guess. Measured on the development machine: primaryMinor 1, 2, 3 for
#     /sys/class/drm/card1..3, matching the PCI bus in the same blocks.
#
#  2. TEMPERATURE, POWER, UTILISATION AND VRAM come from that DRM card's sysfs.
#     Vulkan reports memory heap sizes but no free space (without
#     VK_EXT_memory_budget), no temperature and no power at all - and the fit
#     arithmetic needs used and free VRAM per card, not a heap size.
#
#  The consequence, and it is deliberate: on a card whose driver is not amdgpu
#  there is no second source, so those keys are absent and the card shows '?'
#  for them. It is still detected, still placed, still counted for the fit
#  arithmetic - the model just runs without a thermometer. Reporting a confident
#  zero for a sensor nobody read would be worse.
# ============================================================================
import os
import re
import shutil
import subprocess

NAME = "vulkan"
DEVICE_PREFIX = "Vulkan"
#  ggml's own name for the mask. Same meaning as HIP_VISIBLE_DEVICES: absolute
#  indices, and '--device VulkanN' then counts within what is left.
VISIBLE_ENV = "GGML_VK_VISIBLE_DEVICES"

#  Both overridable, so every card shape can be tested without owning it:
#  the command is replaced by a fixture that prints recorded output, and the
#  sysfs root by a directory tree of small files (see tests/fixtures/mk-vulkan.py).
INFO = os.environ.get("LLM_VULKANINFO", "vulkaninfo")
SYSFS = os.environ.get("LLM_SYSFS_ROOT", "/sys")

#  RADV and amdvlk both put the ISA target in the device name, e.g.
#  "AMD Radeon AI PRO R9700 (RADV GFX1201)". It is the only place a Vulkan
#  device offers one, and the Cards tab has a field for it.
_GFX_RE = re.compile(r"\b(gfx[0-9a-f]{3,4})\b", re.I)
#  "AMD Radeon AI PRO R9700 (RADV GFX1201)" -> "AMD Radeon AI PRO R9700"
_DRIVER_SUFFIX_RE = re.compile(r"\s*\((?:RADV|AMDVLK|LLVM)[^)]*\)\s*$", re.I)


def available() -> bool:
    """Is this backend usable? A Vulkan device has to actually be there.

    `vulkaninfo` existing is not enough: it is installed on machines with no
    usable driver, where it prints an instance and no devices at all.
    """
    if not (shutil.which(INFO) or os.path.exists(INFO)):
        return False
    return any(c.get("discrete") for c in cards().values()) or bool(cards())


def cards() -> dict[int, dict]:
    """The Vulkan devices ggml enumerates, in its order. Keys are absolute.

    Not simply every device vulkaninfo lists. ggml skips PHYSICAL_DEVICE_TYPE_CPU
    - measured: `llama-server --list-devices` on this machine reports Vulkan0..2
    where vulkaninfo lists four, the fourth being llvmpipe - so counting them
    here would shift every index against the one '--device VulkanN' and
    GGML_VK_VISIBLE_DEVICES actually mean.

    It happens to make no difference when the software device sorts last, which
    is where mesa puts it. That is luck, not a guarantee, and this is the bug
    class this project keeps hitting: see the 'llvmpipe-first' fixture, which is
    there because it would pass either way otherwise.
    """
    out: dict[int, dict] = {}
    idx = -1
    for block in _device_blocks():
        kind_raw = _field(block, "deviceType") or ""
        if kind_raw.endswith("_CPU"):
            continue
        idx += 1
        c: dict = {"smiIndex": idx}
        name = _field(block, "deviceName")
        if name:
            gfx = _GFX_RE.search(name)
            if gfx:
                c["gfx"] = gfx.group(1).lower()
            c["name"] = _DRIVER_SUFFIX_RE.sub("", name).strip() or name
        #  DISCRETE_GPU is the only type worth loading a model onto; INTEGRATED
        #  shares system memory. A better signal than rocm-smi's, where the same
        #  distinction has to be guessed from a CPU brand name and a VRAM
        #  threshold.
        c["discrete"] = kind_raw.endswith("DISCRETE_GPU")
        minor = _num(_field(block, "primaryMinor"))
        if isinstance(minor, int):
            c.update(_sysfs(minor))
        out[idx] = c
    return out


def gfx_targets(cards_by_index: dict[int, dict], indices: list[int]) -> str:
    """Nothing. -DGGML_VULKAN=ON compiles SPIR-V, not an ISA per card.

    That is the whole reason this backend is easier to install than ROCm: no
    AMDGPU_TARGETS, no HIP compiler, and therefore no way for the build to be
    wrong about hardware it has not seen.
    """
    return ""


def compiler() -> str | None:
    """Nothing. The Vulkan build uses the ordinary C++ compiler."""
    return None


def missing_hint() -> str:
    return ("No Vulkan device found. The runtime needs a driver (mesa on AMD and\n"
            "  Intel, the proprietary one on NVIDIA) and 'vulkaninfo' to look at it;\n"
            "  building llama.cpp additionally needs the headers and a shader compiler:\n"
            "  sudo apt-get install vulkan-tools libvulkan-dev glslc\n"
            "  then 'llm gpu backend vulkan'")


# ---------------------------------------------------------------------------
#  vulkaninfo
# ---------------------------------------------------------------------------
def _raw() -> str:
    try:
        return subprocess.run([INFO], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _device_blocks() -> list[list[str]]:
    """The per-device sections of the full output, in order.

    Anchored on "Device Properties and Extensions", because 'GPU<n>:' also heads
    the shorter blocks in the layer and group sections further up. A device block
    runs from its own 'GPU<n>:' to the next one.
    """
    lines = _raw().splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("Device Properties and Extensions"):
            start = i
            break
    blocks: list[list[str]] = []
    for line in lines[start:]:
        if re.match(r"^GPU\d+:", line):
            blocks.append([])
        elif blocks:
            blocks[-1].append(line)
    return blocks


def _field(block: list[str], key: str) -> str | None:
    """First 'key = value' in a block. Indentation and spacing vary by version."""
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*(.+?)\s*$")
    for line in block:
        m = pat.match(line)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
#  amdgpu sysfs
# ---------------------------------------------------------------------------
def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _sysfs(minor: int) -> dict:
    """VRAM, utilisation, temperature and power for one DRM card.

    Absent files mean absent keys - a driver that is not amdgpu has none of
    these, and a partial answer is still worth having.
    """
    dev = os.path.join(SYSFS, "class", "drm", "card%d" % minor, "device")
    out: dict = {}
    for key, fname in (("vramTotalBytes", "mem_info_vram_total"),
                       ("vramUsedBytes", "mem_info_vram_used"),
                       ("busyPercent", "gpu_busy_percent")):
        val = _num(_read(os.path.join(dev, fname)))
        if val is not None:
            out[key] = val
    out.update(_hwmon(os.path.join(dev, "hwmon")))
    return out


def _hwmon(base: str) -> dict:
    """Junction temperature in °C and package power in W, from amdgpu's hwmon.

    Both need converting and both have two spellings:
      * tempN_input is millidegrees, and which N is the junction is stated by
        tempN_label. A discrete card has edge/junction/mem; an APU has only
        edge, so it reports no junction temperature rather than the edge one
        under the wrong name.
      * powerN_average (microwatts) on a discrete card, powerN_input on an APU -
        the same split rocm-smi expresses as "Average" versus "Current Socket".
    """
    out: dict = {}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        mon = os.path.join(base, name)
        if (_read(os.path.join(mon, "name")) or "") != "amdgpu":
            continue
        for label_file in sorted(f for f in _listdir(mon) if re.fullmatch(r"temp\d+_label", f)):
            if (_read(os.path.join(mon, label_file)) or "").lower() != "junction":
                continue
            milli = _num(_read(os.path.join(mon, label_file.replace("_label", "_input"))))
            if milli is not None:
                out["tempJunctionC"] = round(milli / 1000.0, 1)
            break
        for power_file in ("power1_average", "power1_input"):
            micro = _num(_read(os.path.join(mon, power_file)))
            if micro is not None:
                out["powerW"] = round(micro / 1e6, 1)
                break
        break
    return out


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


#  See the note in gpu_rocm._num: the same coercion, deliberately duplicated
#  rather than importing from the module that imports this one.
def _num(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
