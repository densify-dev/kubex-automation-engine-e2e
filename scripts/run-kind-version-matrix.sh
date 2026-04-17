#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NEWER_VERSION="${NEWER_VERSION:-v1.35.0}"
OLDER_VERSION="${OLDER_VERSION:-v1.34.0}"

NEWER_CLUSTER_NAME="${NEWER_CLUSTER_NAME:-e2e-135}"
OLDER_CLUSTER_NAME="${OLDER_CLUSTER_NAME:-e2e-134}"

NEWER_NODE_IMAGE="${NEWER_NODE_IMAGE:-kindest/node:${NEWER_VERSION}}"
OLDER_NODE_IMAGE="${OLDER_NODE_IMAGE:-kindest/node:${OLDER_VERSION}}"

run_suite() {
  local label="$1"
  local cluster_name="$2"
  local node_image="$3"

  echo
  echo "=== Running ${label} via run-full-suite.sh on cluster ${cluster_name} (${node_image}) ==="
  CLUSTER_NAME="${cluster_name}" NODE_IMAGE="${node_image}" "${REPO_ROOT}/scripts/run-full-suite.sh"
}

run_suite "kubernetes-${NEWER_VERSION}" "${NEWER_CLUSTER_NAME}" "${NEWER_NODE_IMAGE}"
run_suite "kubernetes-${OLDER_VERSION}" "${OLDER_CLUSTER_NAME}" "${OLDER_NODE_IMAGE}"
