# ComfyUI — image and video generation

ComfyUI lives in `~/comfyui` (override with `LLM_COMFY_HOME`) with its own Python
environment and **PyTorch built for ROCm**, so the Radeon card does the work. The
interface listens on port **8188**.

Install it once:

```bash
bash bin/install-comfyui.sh
```

The script is safe to re-run. It clones ComfyUI if needed, creates the environment,
installs the requirements and forces the ROCm build of torch — but only when torch
is missing or is the CPU/CUDA variant, so a re-run no longer re-downloads ~3 GB.

## Starting and stopping

ComfyUI deliberately does **not** start at boot: it holds VRAM for as long as it
runs.

```bash
llm comfy on          # start
llm comfy off         # stop
llm comfy restart
llm comfy status
llm comfy logs        # live logs
```

To start it automatically anyway: `systemctl --user enable comfyui`.

## Models

ComfyUI models are **not** GGUFs — they are Stable Diffusion / Flux style
checkpoints. They go here:

```
~/comfyui/models/checkpoints/     # main models (SDXL, Flux, SD3, …)
~/comfyui/models/vae/
~/comfyui/models/loras/
~/comfyui/models/clip/            # text encoders (e.g. for Flux)
```

Download a `.safetensors` file from Hugging Face or Civitai into the right folder and
restart ComfyUI. A good starting point is an SDXL checkpoint (~6 GB), which fits
comfortably in 32 GB of VRAM.

## Sharing VRAM with the LLMs

This is the part that bites. ComfyUI gets **one card**, written to
`config/comfyui.env` by `llm gpu sync` and overridable with `LLM_COMFY_GPU`. The LLMs
by default spread across **every** card, so that one card is shared. A large LLM and
image generation at the same time can exhaust it.

What helps:

- **Real separation:** pin a small LLM to a different card
  (`llm add --gpu 1 <repo> <quant>`), and ComfyUI keeps its card to itself. Only the
  large models that need every card still collide.
- llama-swap unloads unused LLMs after 15 idle minutes (`ttl` in the configuration).
- Before a big image session, just stop sending requests to the LLM API — after `ttl`
  the VRAM is free. Or pick a smaller model.
- `llm gpu list` shows what is currently sitting on which card.

## GPU not detected?

Test:

```bash
~/comfyui/venv/bin/python -c "import torch; print(torch.__version__, torch.version.hip, torch.cuda.is_available())"
```

You want something like `2.10.0+rocm7.0  7.0.51831  True`.

- Shows `+cu…` (CUDA) or `None` for hip: the wrong torch is installed.
  ```bash
  bash bin/install-comfyui.sh --deps-only
  ```
  If your ROCm version differs from the wheel index, point at the matching one:
  `ROCM_IDX=https://download.pytorch.org/whl/rocm6.4 bash bin/install-comfyui.sh --deps-only`
- Shows `+rocm…` but `False`: on some RDNA generations an override is needed. Add to
  `~/.config/systemd/user/comfyui.service` under `[Service]`:
  ```
  Environment=HSA_OVERRIDE_GFX_VERSION=12.0.0
  ```
  then `systemctl --user daemon-reload && llm comfy restart`.
- Check that you are in the `render` and `video` groups (`groups`) — otherwise run
  `setup-system.sh` and log in again. `llm doctor` checks this too.

## Updating

```bash
llm update comfy        # tag checkout, dependencies, GPU check, rollback on failure
llm rollback comfy      # back to the recorded ref
```

Custom nodes are never touched, but a node pinned to a newer ComfyUI API can break on
a downgrade — worth a look after rolling back.
