#!/usr/bin/env bash
# ============================================================================
#  Run every suite in this directory
# ============================================================================
#  This is what CI calls, and what CONTRIBUTING.md points at. Nothing here
#  touches the machine's own configuration or its GPUs: the hardware is faked
#  through LLM_ROCM_SMI fixtures and the configuration through a temporary
#  LLM_HOME, so it is safe to run on a live server.
#
#  Run with:  bash tests/run-all.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1

suites=(gpu-matrix config-matrix vram-matrix api-matrix)
failed=()

for s in "${suites[@]}"; do
  printf '\n\033[1;36m══ %s ══\033[0m\n' "$s"
  if bash "tests/$s.sh"; then :; else failed+=("$s"); fi
done

printf '\n\033[1;36m══ result ══\033[0m\n'
if (( ${#failed[@]} == 0 )); then
  printf '\033[0;32mall %d suites passed.\033[0m\n' "${#suites[@]}"
  exit 0
fi
printf '\033[0;31mfailed: %s\033[0m\n' "${failed[*]}"
exit 1
