#!/usr/bin/env bash
# ============================================================================
#  Install or update ComfyUI + PyTorch (ROCm)
# ============================================================================
#  Safe to re-run. At the end it says whether torch sees the card.
#
#    bash bin/install-comfyui.sh              everything: clone, venv, deps, test
#    bash bin/install-comfyui.sh --deps-only  dependencies only (after 'llm update comfy')
#
#  Settings:
#    LLM_COMFY_HOME=~/comfyui                 target directory
#    ROCM_IDX=https://.../whl/rocm7.0         wheel index for the ROCm build of torch
# ============================================================================
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
CU="${LLM_COMFY_HOME:-$HOME/comfyui}"
#  Has to match the installed ROCm version.
ROCM_IDX="${ROCM_IDX:-https://download.pytorch.org/whl/rocm7.0}"
DEPS_ONLY=no
[ "${1:-}" = "--deps-only" ] && DEPS_ONLY=yes

command -v uv >/dev/null || {
  echo "uv is missing. Once:  curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

if [ "$DEPS_ONLY" = no ]; then
  echo "=== 1) Fetching ComfyUI ==="
  [ -d "$CU/.git" ] || git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$CU"
fi
[ -d "$CU/.git" ] || { echo "ComfyUI is not in $CU"; exit 1; }
cd "$CU" || { echo "cannot enter $CU"; exit 1; }

echo "=== 2) venv (Python 3.12) ==="
[ -d "$CU/venv" ] || uv venv --python 3.12 "$CU/venv"
PY="$CU/venv/bin/python"

echo "=== 3) ComfyUI requirements ==="
uv pip install --python "$PY" -r requirements.txt 2>&1 | tail -4

echo "=== 4) PyTorch with ROCm ==="
#  --reinstall pulls ~3 GB and used to run on EVERY invocation. Now only when
#  torch is missing or is the CPU/CUDA build - otherwise a plain install is
#  enough and leaves an existing ROCm build alone.
if "$PY" -c 'import torch,sys; sys.exit(0 if torch.version.hip else 1)' 2>/dev/null; then
  echo "    torch with ROCm is already here ($("$PY" -c 'import torch;print(torch.__version__)')) - not reinstalling."
  uv pip install --python "$PY" --index-url "$ROCM_IDX" \
    torch torchvision torchaudio 2>&1 | tail -3
else
  echo "    no torch with ROCm found - forcing the ROCm build (~3 GB)."
  uv pip install --python "$PY" --index-url "$ROCM_IDX" --reinstall \
    torch torchvision torchaudio 2>&1 | tail -6
fi

echo "=== 5) GPU test ==="
"$PY" - <<'PYEOF'
import torch
print("torch:", torch.__version__, "| ROCm:", torch.version.hip or "no")
ok = torch.cuda.is_available()
print("GPU available:", ok)
if ok:
    print("Device:", torch.cuda.get_device_name(0))
else:
    print("NOTE: if False -> see docs/COMFYUI.md, 'GPU not detected'.")
PYEOF
echo "=== DONE ==="
