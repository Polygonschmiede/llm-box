#!/usr/bin/env python3
"""Generate a fake Vulkan machine: vulkaninfo output plus an amdgpu sysfs tree.

Why two things: the Vulkan backend reads identity and device order from
`vulkaninfo` and everything measurable - VRAM, utilisation, temperature, power -
from the DRM card's sysfs, because Vulkan reports none of it. So a fixture has
to fake both, and the interesting part is the join between them.

That join is VkPhysicalDeviceDrmPropertiesEXT.primaryMinor, and these fixtures
deliberately **permute** it: Vulkan device 0 is /sys/class/drm/card3, device 1 is
card1, device 2 is card2. Anything that pairs the two lists by position instead
of by minor therefore reads the wrong card's temperature, and the test says so.
The numbering bug class this project keeps hitting is exactly that, which is why
the fixture is hostile about it.

The card shapes mirror tests/fixtures/mk-smi.py case for case, and the sensor
values are the same numbers, so gpu-matrix can assert the SAME expectations
against both backends - which is the actual claim: one answer, two sources.

One simplification: every card here reports a junction temperature, including
the APU, because the ROCm fixtures do. Real APUs report only 'edge', and the
case of a card with no telemetry at all has its own fixture ('no-sysfs') rather
than being smuggled in here.

Usage:  python3 tests/fixtures/mk-vulkan.py
        LLM_FIXTURE_DIR=/tmp/x python3 .../mk-vulkan.py
Used as: LLM_BACKEND=vulkan LLM_VULKANINFO=.../vulkaninfo-2card.sh \\
         LLM_SYSFS_ROOT=.../sysfs-2card python3 lib/llmreg.py gpus
"""

import os
import stat

HERE = os.environ.get("LLM_FIXTURE_DIR") or os.path.dirname(os.path.abspath(__file__))

#  vram/gfx match mk-smi.py; 'kind' is what Vulkan states and ROCm has to guess.
R9700 = {"name": "AMD Radeon AI PRO R9700 (RADV GFX1201)", "kind": "DISCRETE_GPU",
         "vram": 34208743424, "sysfs": True}
W7900 = {"name": "AMD Radeon PRO W7900 (RADV GFX1100)", "kind": "DISCRETE_GPU",
         "vram": 51539607552, "sysfs": True}
IGPU = {"name": "AMD Ryzen 7 7700X 8-Core Processor (RADV RAPHAEL_MENDOCINO)",
        "kind": "INTEGRATED_GPU", "vram": 536870912, "sysfs": True}
APU_BIG = {"name": "AMD Ryzen 7 8700G w/ Radeon 780M Graphics (RADV PHOENIX)",
           "kind": "INTEGRATED_GPU", "vram": 17179869184, "sysfs": True}
#  Vulkan's software rasteriser. It is a real Vulkan device, it is offered on
#  every machine with mesa, and running a model on it would be a disaster - so
#  the detection has to exclude it, and here it is to prove that it does.
LLVMPIPE = {"name": "llvmpipe (LLVM 21.1.8, 256 bits)", "kind": "CPU",
            "vram": 0, "sysfs": False}
#  A discrete card whose driver is not amdgpu: identity from Vulkan, no sysfs, so
#  no temperature, no watts, no VRAM. It still has to be detected and counted.
FOREIGN = {"name": "Intel(R) Arc(tm) A770 Graphics (DG2)", "kind": "DISCRETE_GPU",
           "vram": 0, "sysfs": False}

CASES = {
    "1card":      [R9700],
    "2card":      [R9700, R9700],
    "3card":      [R9700, R9700, R9700],
    "igpu-last":  [R9700, R9700, IGPU],
    "igpu-first": [IGPU, R9700, R9700],
    "apu-16gb":   [APU_BIG, R9700],
    "mixed":      [R9700, W7900],
    "none":       [],
    #  Vulkan-only shapes, with no counterpart under ROCm.
    "llvmpipe":   [R9700, IGPU, LLVMPIPE],
    #  The same devices with the software one FIRST. ggml skips CPU-type devices,
    #  so it sees the two R9700s as Vulkan0 and Vulkan1 - and anything that
    #  counted every vulkaninfo entry would call them 1 and 2 and then address the
    #  wrong card. With llvmpipe last, as mesa puts it, both are indistinguishable.
    "llvmpipe-first": [LLVMPIPE, R9700, R9700],
    "no-sysfs":   [FOREIGN, FOREIGN],
}

#  Vulkan device index -> DRM minor. Permuted on purpose; see the docstring.
MINORS = [3, 1, 2, 4, 5, 6]


def vulkaninfo(cards: list[dict]) -> str:
    """The parts of `vulkaninfo` the backend reads, in the shape it reads them.

    Not the whole 8000 lines: the header down to "Device Properties and
    Extensions" is what anchors the parse, the 'GPU<n>:' lines delimit devices,
    and within a device only deviceType, deviceName and primaryMinor are used.
    The surrounding noise is here because the parser has to skip it.
    """
    out = ["==========", "VULKANINFO", "==========", "",
           "Vulkan Instance Version: 1.4.341", "",
           "Instance Extensions: count = 2", "-------------------------------",
           "VK_KHR_surface                         : extension revision 25",
           "VK_KHR_display                         : extension revision 23", "",
           #  A 'GPU0:' block BEFORE the device section, which is what the
           #  anchor exists for: parsing from the top would read this one.
           "Device Groups:", "==============", "Group 0:", "\tProperties:",
           "\t\tphysicalDevices: count = 1", "",
           "Device Properties and Extensions:", "=================================="]
    for i, c in enumerate(cards):
        out += [
            "GPU%d:" % i,
            "VkPhysicalDeviceProperties:",
            "---------------------------",
            "\tapiVersion        = 1.4.335",
            "\tdriverVersion     = 26.0.8",
            "\tvendorID          = 0x1002",
            "\tdeviceID          = 0x7551",
            "\tdeviceType        = PHYSICAL_DEVICE_TYPE_%s" % c["kind"],
            "\tdeviceName        = %s" % c["name"],
            "",
        ]
        if c["sysfs"]:
            out += [
                "VkPhysicalDeviceDrmPropertiesEXT:",
                "---------------------------------",
                "\thasPrimary   = true",
                "\tprimaryMajor = 226",
                "\tprimaryMinor = %d" % MINORS[i],
                "",
                "VkPhysicalDevicePCIBusInfoPropertiesEXT:",
                "----------------------------------------",
                "\tpciDomain   = 0",
                "\tpciBus      = %d" % (3 + i * 10),
                "",
            ]
    return "\n".join(out) + "\n"


def sysfs(root: str, cards: list[dict]) -> int:
    """Write the amdgpu files the backend reads. Returns how many cards got them."""
    made = 0
    for i, c in enumerate(cards):
        if not c["sysfs"]:
            continue
        dev = os.path.join(root, "class", "drm", "card%d" % MINORS[i], "device")
        mon = os.path.join(dev, "hwmon", "hwmon%d" % (8 + i))
        os.makedirs(mon, exist_ok=True)
        used = 171778048 if i == 0 else 73072640
        _w(dev, "mem_info_vram_total", c["vram"])
        _w(dev, "mem_info_vram_used", used)
        _w(dev, "gpu_busy_percent", 0 if i else 42)
        _w(mon, "name", "amdgpu")
        #  Three sensors, and only the labelled one is the junction. temp1 is
        #  'edge' and reads differently on purpose: picking the first sensor
        #  instead of the labelled one would show 30 °C everywhere.
        _w(mon, "temp1_label", "edge")
        _w(mon, "temp1_input", 30000)
        _w(mon, "temp2_label", "junction")
        _w(mon, "temp2_input", int((29.0 + i) * 1000))
        _w(mon, "temp3_label", "mem")
        _w(mon, "temp3_input", 34000)
        #  Discrete cards expose power1_average, an APU power1_input - the same
        #  split rocm-smi words as "Average" versus "Current Socket".
        _w(mon, "power1_average" if c["kind"] == "DISCRETE_GPU" else "power1_input",
           int((17.0 + i) * 1e6))
        #  A second hwmon that is NOT amdgpu, because the real tree has those
        #  (nvme, k10temp) and the reader has to skip them.
        other = os.path.join(dev, "hwmon", "hwmon%d" % (90 + i))
        os.makedirs(other, exist_ok=True)
        _w(other, "name", "not-amdgpu")
        _w(other, "temp1_label", "junction")
        _w(other, "temp1_input", 99000)
        made += 1
    return made


def _w(directory: str, name: str, value) -> None:
    with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
        fh.write("%s\n" % value)


for case, cards in CASES.items():
    path = os.path.join(HERE, "vulkaninfo-%s.sh" % case)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n# GENERATED by mk-vulkan.py - do not edit by hand.\n")
        fh.write("cat <<'VKI'\n%sVKI\n" % vulkaninfo(cards))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    root = os.path.join(HERE, "sysfs-%s" % case)
    made = sysfs(root, cards)
    print("  %-26s %d device(s), %d with amdgpu sysfs"
          % (os.path.basename(path), len(cards), made))
