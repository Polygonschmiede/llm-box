"""What 'llm gpu sync' writes to disk.

gpu_sync() and write_env() were reached by no test at all, and between them they
produce the two files the systemd units read - so an error here does not show up
as a wrong answer but as services that see the wrong cards after the next reboot.
"""
import os
import stat


def _env(path):
    """A KEY=value file as a dict, comments dropped."""
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_hardware_env_names_the_backend_and_the_mask(load, fixtures):
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    reg.write_env()
    got = _env(reg.HARDWARE_ENV)
    assert got["LLM_BACKEND"] == "rocm"
    assert got["HIP_VISIBLE_DEVICES"] == "0,1"
    assert got["LLM_GFX_TARGETS"] == "gfx1201"
    #  Written even when empty: lib/update.sh reads these with hw_get, where an
    #  absent key and an empty one mean the same thing, while a MISSING LINE reads
    #  as "this file predates the backend that needs it".
    assert "LLM_HIP_COMPILER" in got
    #  And NOT written: nothing read LLM_TENSOR_SPLIT - not bin/llm, not
    #  lib/update.sh, and not llama.cpp, which knows no such name. The units
    #  export this file into every service, so a dead variable is not free.
    assert "LLM_TENSOR_SPLIT" not in got
    #  The value itself is still reported, just not as an environment variable.
    assert reg.hw()["tensorSplit"] == "1,1"


def test_only_one_mask_is_ever_written(load, fixtures):
    """Both names in one file would be two answers to one question."""
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    reg.write_env()
    rocm = _env(reg.HARDWARE_ENV)
    assert "GGML_VK_VISIBLE_DEVICES" not in rocm
    #  And after a switch the old name is gone rather than left behind, because
    #  the file is rewritten whole.
    reg = load(LLM_BACKEND="vulkan", LLM_VULKANINFO=fixtures("vulkaninfo-2card.sh"),
               LLM_SYSFS_ROOT=fixtures("sysfs-2card", directory=True))
    reg.write_env()
    vk = _env(reg.HARDWARE_ENV)
    assert vk["GGML_VK_VISIBLE_DEVICES"] == "0,1"
    assert "HIP_VISIBLE_DEVICES" not in vk
    assert vk["LLM_BACKEND"] == "vulkan"


def test_the_absolute_number_is_written_not_the_logical_one(load, fixtures):
    """The bug class this project keeps hitting, at the place it does damage.

    With the iGPU first the compute cards are absolute 1 and 2, and that is what
    the mask has to say. Writing the logical 0,1 would hand the services the iGPU.
    """
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-igpu-first.sh"))
    reg.write_env()
    assert _env(reg.HARDWARE_ENV)["HIP_VISIBLE_DEVICES"] == "1,2"


def test_comfyui_gets_exactly_one_card(load, fixtures):
    """It holds VRAM for as long as it runs, so it is given one card and no more."""
    smi = fixtures("rocm-smi-igpu-first.sh")
    reg = load(LLM_ROCM_SMI=smi)
    reg.write_env()
    #  Absolute again, and the FIRST compute card rather than absolute 0.
    assert _env(reg.COMFY_ENV)["HIP_VISIBLE_DEVICES"] == "1"
    reg = load(LLM_ROCM_SMI=smi, LLM_COMFY_GPU="1")
    reg.write_env()
    assert _env(reg.COMFY_ENV)["HIP_VISIBLE_DEVICES"] == "2"


def test_comfyui_env_stays_hip_named_under_vulkan(load, fixtures):
    """ComfyUI runs on a ROCm torch wheel; there is no Vulkan one.

    So this file is meaningless under Vulkan rather than differently spelled, and
    renaming the variable would suggest otherwise.
    """
    reg = load(LLM_BACKEND="vulkan", LLM_VULKANINFO=fixtures("vulkaninfo-2card.sh"),
               LLM_SYSFS_ROOT=fixtures("sysfs-2card", directory=True))
    reg.write_env()
    assert "HIP_VISIBLE_DEVICES" in _env(reg.COMFY_ENV)


def test_no_cards_writes_an_empty_mask_not_a_broken_file(load, fixtures):
    """A machine whose driver is not answering. The file has to stay parseable:
    the units read it with EnvironmentFile=-, and a mask of "" means "do not
    restrict", which is the right behaviour when nothing was detected."""
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-none.sh"))
    reg.write_env()
    got = _env(reg.HARDWARE_ENV)
    assert got["HIP_VISIBLE_DEVICES"] == ""
    assert got["LLM_BACKEND"] == "rocm"


def test_gpu_sync_rewrites_the_device_prefix(load, fixtures, add_block):
    add_block("big", "${server} -m /m/big.gguf --device ROCm1 -sm none -mg 0")
    reg = load(LLM_BACKEND="vulkan", LLM_VULKANINFO=fixtures("vulkaninfo-2card.sh"),
               LLM_SYSFS_ROOT=fixtures("sysfs-2card", directory=True))
    out = reg.gpu_sync()
    assert out["backend"] == "vulkan"
    assert out["configChanged"] is True
    assert "--device Vulkan1" in reg.config_text()
    assert "--device ROCm1" not in reg.config_text()


def test_gpu_sync_dry_run_writes_nothing(load, fixtures, add_block):
    """The control page previews every write through this, so it has to be inert."""
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    add_block("big", "${server} -m /m/big.gguf --device ROCm1 -sm none -mg 0")
    before = reg.config_text()
    out = reg.gpu_sync(dry_run=True)
    assert out["dryRun"] is True
    assert reg.config_text() == before
    assert not os.path.exists(reg.HARDWARE_ENV)


def test_gpu_sync_is_idempotent(load, fixtures, add_block):
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    add_block("big", "${server} -m /m/big.gguf --device ROCm1 -sm none -mg 0")
    reg.gpu_sync()
    once = reg.config_text()
    assert reg.gpu_sync()["configChanged"] is False
    assert reg.config_text() == once


def test_the_env_files_are_not_secret(load, fixtures):
    """Card numbers, not credentials - but they must be READABLE, because systemd
    reads them as the same user and a stray 0600 here would be a service that
    starts without seeing any card."""
    reg = load(LLM_ROCM_SMI=fixtures("rocm-smi-2card.sh"))
    reg.write_env()
    for p in (reg.HARDWARE_ENV, reg.COMFY_ENV):
        assert stat.S_IMODE(os.stat(p).st_mode) & stat.S_IRUSR
