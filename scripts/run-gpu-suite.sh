#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

GPU_SUITE="${GPU_SUITE:-true}"
GPU_KIND_CONFIG="${GPU_KIND_CONFIG:-${E2E_ROOT}/features/gpu/kind-config.yaml}"

GPU_SUITE="${GPU_SUITE}" \
GPU_KIND_CONFIG="${GPU_KIND_CONFIG}" \
"${E2E_ROOT}/scripts/run-full-suite.sh" tests/test_gpu_kai.py "$@"
