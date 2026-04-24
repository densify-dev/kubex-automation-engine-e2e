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

if [[ "$IMG" != *:* ]]; then
  IMG="${IMG}:latest"
fi

run_suite() {
  local label="$1"
  local cluster_name="$2"
  local node_image="$3"
  local with_metrics_server="${4:-true}"
  local with_keda="${5:-true}"
  local with_vpa="${6:-true}"

  echo
  echo "=== Running ${label} via run-full-suite.sh on cluster ${cluster_name} (${node_image}) ==="
  CLUSTER_NAME="${cluster_name}" \
    NODE_IMAGE="${node_image}" \
    WITH_METRICS_SERVER="${with_metrics_server}" \
    WITH_KEDA="${with_keda}" \
    WITH_VPA="${with_vpa}" \
    EXAMPLES_ROOT="${CONTROLLER_ROOT}/examples" \
    RECOMMENDATIONS_FILE="${CONTROLLER_ROOT}/examples/recommendations.json" \
    HELM_CRDS_CHART="${CONTROLLER_ROOT}/charts/kubex-crds" \
    HELM_CONTROLLER_CHART="${CONTROLLER_ROOT}/charts/kubex-automation-engine" \
    HELM_REPO_URL="" \
    CONTROLLER_IMAGE_REPOSITORY="${IMG%:*}" \
    CONTROLLER_IMAGE_TAG="${IMG##*:}" \
    LOAD_KIND_IMAGES=true \
    "${E2E_ROOT}/scripts/run-full-suite.sh"
}

echo "==> Building local controller images"
make -C "${CONTROLLER_ROOT}" docker-build docker-build-cleanup IMG="${IMG}"

echo "==> Running the full Kind version matrix"
run_suite "kubernetes-${NEWER_VERSION}" "${NEWER_CLUSTER_NAME}" "${NEWER_NODE_IMAGE}"
run_suite "kubernetes-${OLDER_VERSION}" "${OLDER_CLUSTER_NAME}" "${OLDER_NODE_IMAGE}" "true" "false" "false"
