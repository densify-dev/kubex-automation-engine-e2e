#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONTROLLER_ROOT="$(cd "${E2E_ROOT}/../.." && pwd)"

NEWER_VERSION="${NEWER_VERSION:-v1.35.0}"
OLDER_VERSION="${OLDER_VERSION:-v1.32.0}"

NEWER_CLUSTER_NAME="${NEWER_CLUSTER_NAME:-e2e-135}"
OLDER_CLUSTER_NAME="${OLDER_CLUSTER_NAME:-e2e-132}"

NEWER_NODE_IMAGE="${NEWER_NODE_IMAGE:-kindest/node:${NEWER_VERSION}}"
OLDER_NODE_IMAGE="${OLDER_NODE_IMAGE:-kindest/node:${OLDER_VERSION}}"

IMG="${IMG:-densify/automation-controller:latest}"
CLEANUP_IMG="${CLEANUP_IMG:-registry.automation-controller.local:32000/kubex-cleanup:latest}"

normalize_target() {
  local target="$1"
  if [[ "$target" == test/e2e/* ]]; then
    printf '%s\n' "${target#test/e2e/}"
  else
    printf '%s\n' "$target"
  fi
}

if [[ "$IMG" != *:* ]]; then
  IMG="${IMG}:latest"
fi
if [[ "$CLEANUP_IMG" != *:* ]]; then
  CLEANUP_IMG="${CLEANUP_IMG}:latest"
fi

reset_cluster() {
  local cluster_name="$1"

  if kind get clusters | grep -Fxq "${cluster_name}"; then
    echo "==> Deleting existing Kind cluster ${cluster_name}"
    if ! kind delete cluster --name "${cluster_name}"; then
      echo "ERROR: Failed to delete Kind cluster '${cluster_name}'. Aborting." >&2
      exit 1
    fi
  fi
}

run_suite() {
  local label="$1"
  local cluster_name="$2"
  local node_image="$3"
  local with_metrics_server="${4:-true}"
  local with_keda="${5:-true}"
  local with_vpa="${6:-true}"
  local gpu_suite="${7:-true}"
  local gpu_kind_config="${8:-${E2E_ROOT}/features/gpu/kind-config.yaml}"
  local pytest_targets=("${@:9}")

  echo
  echo "=== Running ${label} via run-full-suite.sh on cluster ${cluster_name} (${node_image}) ==="
  CLUSTER_NAME="${cluster_name}" \
    NODE_IMAGE="${node_image}" \
    WITH_METRICS_SERVER="${with_metrics_server}" \
    WITH_KEDA="${with_keda}" \
    WITH_VPA="${with_vpa}" \
    GPU_SUITE="${gpu_suite}" \
    GPU_KIND_CONFIG="${gpu_kind_config}" \
    EXAMPLES_ROOT="${CONTROLLER_ROOT}/examples" \
    HELM_CRDS_CHART="${CONTROLLER_ROOT}/charts/kubex-crds" \
    HELM_CONTROLLER_CHART="${CONTROLLER_ROOT}/charts/kubex-automation-engine" \
    HELM_REPO_URL="" \
    CONTROLLER_IMAGE_REPOSITORY="${IMG%:*}" \
    CONTROLLER_IMAGE_TAG="${IMG##*:}" \
    CLEANUP_IMAGE_REPOSITORY="${CLEANUP_IMG%:*}" \
    CLEANUP_IMAGE_TAG="${CLEANUP_IMG##*:}" \
    LOAD_KIND_IMAGES=true \
    "${E2E_ROOT}/scripts/run-full-suite.sh" ${pytest_targets[@]+"${pytest_targets[@]}"}
}

echo "==> Building local controller images"
make -C "${CONTROLLER_ROOT}" docker-build docker-build-cleanup IMG="${IMG}" CLEANUP_IMG="${CLEANUP_IMG}"

echo "==> Running the full Kind version matrix"
reset_cluster "${NEWER_CLUSTER_NAME}"
run_suite "kubernetes-${NEWER_VERSION}" "${NEWER_CLUSTER_NAME}" "${NEWER_NODE_IMAGE}" "true" "true" "true" "true" "${E2E_ROOT}/features/gpu/kind-config.yaml" "$(normalize_target "$1")"
reset_cluster "${OLDER_CLUSTER_NAME}"
run_suite "kubernetes-${OLDER_VERSION}" "${OLDER_CLUSTER_NAME}" "${OLDER_NODE_IMAGE}" "true" "false" "false" "true" "${E2E_ROOT}/features/gpu/kind-config.yaml" "$(normalize_target "$1")"
