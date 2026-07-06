#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REPO_ROOT}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"
REQ_FILE="${REQ_FILE:-${REPO_ROOT}/requirements.txt}"
REQ_STAMP="${VENV_DIR}/.requirements.sha256"

PYTEST_BIN="${PYTEST_BIN:-${VENV_PYTHON}}"
PYTEST_WORKERS="${PYTEST_WORKERS:-}"
KEEP_KIND_CLUSTER="${KEEP_KIND_CLUSTER:-}"

CLUSTER_NAME="${CLUSTER_NAME:-e2e}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
NODE_IMAGE="${NODE_IMAGE:-kindest/node:v1.35.0}"

CONTROLLER_IMAGE_REPOSITORY="${CONTROLLER_IMAGE_REPOSITORY:-}"
CONTROLLER_IMAGE_TAG="${CONTROLLER_IMAGE_TAG:-}"
CONTROLLER_IMAGE_PULL_POLICY="${CONTROLLER_IMAGE_PULL_POLICY:-IfNotPresent}"
CLEANUP_IMAGE_REPOSITORY="${CLEANUP_IMAGE_REPOSITORY:-}"
CLEANUP_IMAGE_TAG="${CLEANUP_IMAGE_TAG:-}"
CLEANUP_IMAGE_PULL_POLICY="${CLEANUP_IMAGE_PULL_POLICY:-IfNotPresent}"
LOAD_KIND_IMAGES="${LOAD_KIND_IMAGES:-}"
HELM_CRDS_CHART="${HELM_CRDS_CHART:-}"
HELM_CONTROLLER_CHART="${HELM_CONTROLLER_CHART:-}"
HELM_CRDS_CHART_VERSION="${HELM_CRDS_CHART_VERSION:-}"
HELM_CONTROLLER_CHART_VERSION="${HELM_CONTROLLER_CHART_VERSION:-}"
KUBEX_URL_HOST="${KUBEX_URL_HOST:-}"
KUBEX_URL_SCHEME="${KUBEX_URL_SCHEME:-}"
KUBEAI_CHART_VERSION="${KUBEAI_CHART_VERSION:-}"
INSTALL_KUBEAI="${INSTALL_KUBEAI:-}"
DEPLOY_KUBEX_STUB="${DEPLOY_KUBEX_STUB:-true}"
GPU_SUITE="${GPU_SUITE:-false}"
GPU_KIND_CONFIG="${GPU_KIND_CONFIG:-}"

is_true() {
  [[ "$1" == "1" ]] || [[ "$1" == "true" ]]
}

if [[ -z "${RECOMMENDATIONS_FILE:-}" ]]; then
  if is_true "$DEPLOY_KUBEX_STUB"; then
    RECOMMENDATIONS_FILE=""
  else
    RECOMMENDATIONS_FILE="${REPO_ROOT}/examples/recommendations.json"
  fi
fi

WITH_METRICS_SERVER="${WITH_METRICS_SERVER:-true}"
WITH_KEDA="${WITH_KEDA:-true}"
WITH_VPA="${WITH_VPA:-true}"

HELM_RELEASE="${HELM_RELEASE:-kubex-automation-engine}"
HELM_NAMESPACE="${HELM_NAMESPACE:-kubex}"
HELM_REPO_URL="${HELM_REPO_URL:-}"

log() {
  echo
  echo "==> $*"
}

run_cmd() {
  echo "+ $*"
  "$@"
}

append_flag_if_set() {
  local value="$1"
  local flag="$2"
  local array_name="$3"

  if [[ -n "$value" ]]; then
    local -n target="$array_name"
    target+=("$flag" "$value")
  fi
}

ensure_python_env() {
  local python_bin="${PYTHON_BIN:-python3}"
  local requirements_hash
  requirements_hash="$(sha256sum "$REQ_FILE" | awk '{print $1}')"

  if [[ ! -x "$VENV_PYTHON" ]]; then
    log "Creating Python virtual environment at ${VENV_DIR}"
    run_cmd "$python_bin" -m venv "$VENV_DIR"
  fi

  if [[ ! -f "$REQ_STAMP" ]] || [[ "$(cat "$REQ_STAMP")" != "$requirements_hash" ]]; then
    log "Installing Python dependencies from ${REQ_FILE}"
    run_cmd "$VENV_PIP" install -r "$REQ_FILE"
    printf '%s\n' "$requirements_hash" >"$REQ_STAMP"
  else
    log "Python dependencies already match ${REQ_FILE}"
  fi
}

wait_for_release_absent() {
  local release="$1"
  local timeout_seconds="${2:-180}"
  local deadline=$((SECONDS + timeout_seconds))
  until ! helm status "$release" --kube-context "$KUBE_CONTEXT" --namespace "$HELM_NAMESPACE" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "Timed out waiting for Helm release ${release} to be removed" >&2
      helm status "$release" --kube-context "$KUBE_CONTEXT" --namespace "$HELM_NAMESPACE" || true
      kubectl --context "$KUBE_CONTEXT" -n "$HELM_NAMESPACE" get all || true
      return 1
    fi
    sleep 2
  done
}

wait_for_controller_pods_absent() {
  local timeout_seconds="${1:-180}"
  local deadline=$((SECONDS + timeout_seconds))
  until [ -z "$(kubectl --context "$KUBE_CONTEXT" -n "$HELM_NAMESPACE" get pods -l control-plane=controller-manager -o name 2>/dev/null)" ]; do
    if (( SECONDS >= deadline )); then
      echo "Timed out waiting for controller-manager pods to be removed" >&2
      kubectl --context "$KUBE_CONTEXT" -n "$HELM_NAMESPACE" get pods -l control-plane=controller-manager -o wide || true
      kubectl --context "$KUBE_CONTEXT" -n "$HELM_NAMESPACE" get events --sort-by=.lastTimestamp || true
      return 1
    fi
    sleep 2
  done
}

wait_for_crd_absent() {
  local crd_name="$1"
  local timeout_seconds="${2:-180}"
  local deadline=$((SECONDS + timeout_seconds))
  until ! kubectl --context "$KUBE_CONTEXT" get crd "$crd_name" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "Timed out waiting for CRD ${crd_name} to be removed" >&2
      kubectl --context "$KUBE_CONTEXT" get crd "$crd_name" -o yaml || true
      return 1
    fi
    sleep 2
  done
}

cleanup_rightsizing_resources() {
  local namespaced_resources=(
    automationstrategies
    proactivepolicies
    staticpolicies
    rollbackpolicies
    gpurebalancingpolicies
  )
  local cluster_resources=(
    clusterautomationstrategies
    clusterproactivepolicies
    clusterstaticpolicies
    clusterrollbackpolicies
    clustergpurebalancingpolicies
    globalconfigurations
    policyevaluations
    gpuconsolidationpolicies
    podaffinitypolicies
  )

  log "Deleting lingering rightsizing CRs before controller uninstall"
  for resource in "${namespaced_resources[@]}"; do
    run_cmd kubectl --context "$KUBE_CONTEXT" delete --ignore-not-found --wait=false --all --all-namespaces "$resource"
  done
  for resource in "${cluster_resources[@]}"; do
    run_cmd kubectl --context "$KUBE_CONTEXT" delete --ignore-not-found --wait=false --all "$resource"
  done

  local timeout_seconds="${1:-180}"
  local deadline=$((SECONDS + timeout_seconds))
  local remaining

  while (( SECONDS < deadline )); do
    remaining=""
    for resource in "${namespaced_resources[@]}"; do
      if [[ -n "$(kubectl --context "$KUBE_CONTEXT" get "$resource" --all-namespaces -o name 2>/dev/null)" ]]; then
        remaining="$resource"
        break
      fi
    done
    if [[ -z "$remaining" ]]; then
      for resource in "${cluster_resources[@]}"; do
        if [[ -n "$(kubectl --context "$KUBE_CONTEXT" get "$resource" -o name 2>/dev/null)" ]]; then
          remaining="$resource"
          break
        fi
      done
    fi
    if [[ -z "$remaining" ]]; then
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for rightsizing resources to be removed before controller uninstall" >&2
  for resource in "${namespaced_resources[@]}"; do
    kubectl --context "$KUBE_CONTEXT" get "$resource" --all-namespaces -o wide || true
  done
  for resource in "${cluster_resources[@]}"; do
    kubectl --context "$KUBE_CONTEXT" get "$resource" -o wide || true
  done
  return 1
}

bootstrap_cluster() {
  local bootstrap_args=(
    --kube-context "$KUBE_CONTEXT"
    --kind-cluster-name "$CLUSTER_NAME"
    --namespace "$HELM_NAMESPACE"
    --helm-release "$HELM_RELEASE"
  )
  append_flag_if_set "$CONTROLLER_IMAGE_REPOSITORY" --controller-image-repository bootstrap_args
  append_flag_if_set "$CONTROLLER_IMAGE_TAG" --controller-image-tag bootstrap_args
  append_flag_if_set "$CLEANUP_IMAGE_REPOSITORY" --cleanup-image-repository bootstrap_args
  append_flag_if_set "$CLEANUP_IMAGE_TAG" --cleanup-image-tag bootstrap_args
  append_flag_if_set "$CLEANUP_IMAGE_PULL_POLICY" --cleanup-image-pull-policy bootstrap_args
  append_flag_if_set "$NODE_IMAGE" --kind-node-image bootstrap_args
  append_flag_if_set "$RECOMMENDATIONS_FILE" --recommendations-file bootstrap_args
  append_flag_if_set "$HELM_REPO_URL" --helm-repo-url bootstrap_args
  append_flag_if_set "$HELM_CRDS_CHART" --helm-crds-chart bootstrap_args
  append_flag_if_set "$HELM_CONTROLLER_CHART" --helm-controller-chart bootstrap_args
  append_flag_if_set "$HELM_CRDS_CHART_VERSION" --helm-crds-chart-version bootstrap_args
  append_flag_if_set "$HELM_CONTROLLER_CHART_VERSION" --helm-controller-chart-version bootstrap_args
  append_flag_if_set "$KUBEX_URL_HOST" --kubex-url-host bootstrap_args
  append_flag_if_set "$KUBEX_URL_SCHEME" --kubex-url-scheme bootstrap_args
  append_flag_if_set "$KUBEAI_CHART_VERSION" --kubeai-chart-version bootstrap_args
  if is_true "$INSTALL_KUBEAI"; then
    bootstrap_args+=(--install-kubeai)
  fi
  if is_true "$DEPLOY_KUBEX_STUB"; then
    bootstrap_args+=(--deploy-kubex-stub)
  fi
  if is_true "$GPU_SUITE"; then
    bootstrap_args+=(--gpu-suite)
    if [[ -n "$GPU_KIND_CONFIG" ]]; then
      bootstrap_args+=(--gpu-kind-config "$GPU_KIND_CONFIG")
    fi
  fi
  if is_true "$LOAD_KIND_IMAGES"; then
    bootstrap_args+=(--load-kind-images)
  fi
  if ! is_true "$WITH_METRICS_SERVER"; then
    bootstrap_args+=(--without-metrics-server)
  fi
  if ! is_true "$WITH_KEDA"; then
    bootstrap_args+=(--without-keda)
  fi
  if ! is_true "$WITH_VPA"; then
    bootstrap_args+=(--without-vpa)
  fi

  log "Bootstrapping cluster ${KUBE_CONTEXT}"
  run_cmd "$VENV_PYTHON" -m bootstrap "${bootstrap_args[@]}"
}

run_functional_suite() {
  local pytest_targets=("$@")
  if [[ ${#pytest_targets[@]} -eq 0 ]]; then
    pytest_targets=(tests/)
  fi

  local args=(
    -v
    -rs
    --skip-kind-bootstrap
    --kube-context "$KUBE_CONTEXT"
    --kind-cluster-name "$CLUSTER_NAME"
    --namespace "$HELM_NAMESPACE"
    --helm-release "$HELM_RELEASE"
    --controller-image-pull-policy "$CONTROLLER_IMAGE_PULL_POLICY"
    --test-namespace e2e-test
    --timeout 120
  )
  if ! is_true "$GPU_SUITE"; then
    args+=(-m "not gpu_suite")
  fi
  if [[ -n "$PYTEST_WORKERS" ]]; then
    args+=(-n "$PYTEST_WORKERS")
  fi
  append_flag_if_set "$CONTROLLER_IMAGE_REPOSITORY" --controller-image-repository args
  append_flag_if_set "$CONTROLLER_IMAGE_TAG" --controller-image-tag args
  append_flag_if_set "$NODE_IMAGE" --kind-node-image args
  append_flag_if_set "$RECOMMENDATIONS_FILE" --recommendations-file args
  append_flag_if_set "$HELM_CRDS_CHART" --helm-crds-chart args
  append_flag_if_set "$HELM_CONTROLLER_CHART" --helm-controller-chart args
  append_flag_if_set "$HELM_CRDS_CHART_VERSION" --helm-crds-chart-version args
  append_flag_if_set "$HELM_CONTROLLER_CHART_VERSION" --helm-controller-chart-version args
  append_flag_if_set "$KUBEX_URL_HOST" --kubex-url-host args
  append_flag_if_set "$KUBEX_URL_SCHEME" --kubex-url-scheme args
  append_flag_if_set "$KUBEAI_CHART_VERSION" --kubeai-chart-version args
  if is_true "$DEPLOY_KUBEX_STUB"; then
    args+=(--deploy-kubex-stub)
  fi
  if is_true "$GPU_SUITE"; then
    args+=(--gpu-suite)
    if [[ -n "$GPU_KIND_CONFIG" ]]; then
      args+=(--gpu-kind-config "$GPU_KIND_CONFIG")
    fi
  fi
  if is_true "$KEEP_KIND_CLUSTER"; then
    args+=(--keep-kind-cluster)
  fi
  args+=("${pytest_targets[@]}")

  log "Running functional suite on ${KUBE_CONTEXT}"
  echo "+ ${PYTEST_BIN} -m pytest $(printf '%q ' "${args[@]}")"
  run_cmd "${PYTEST_BIN}" -m pytest "${args[@]}"
}

verify_uninstall() {
  cleanup_rightsizing_resources

  log "Uninstalling controller Helm release ${HELM_RELEASE}"
  run_cmd helm uninstall "$HELM_RELEASE" --kube-context "$KUBE_CONTEXT" --namespace "$HELM_NAMESPACE"

  log "Verifying controller release removal"
  wait_for_release_absent "$HELM_RELEASE"
  wait_for_controller_pods_absent

  log "Uninstalling CRD Helm release kubex-crds"
  run_cmd helm uninstall kubex-crds --kube-context "$KUBE_CONTEXT" --namespace "$HELM_NAMESPACE"

  log "Verifying CRD removal"
  local crds=(
    automationstrategies.rightsizing.kubex.ai
    clusterautomationstrategies.rightsizing.kubex.ai
    globalconfigurations.rightsizing.kubex.ai
    policyevaluations.rightsizing.kubex.ai
    staticpolicies.rightsizing.kubex.ai
    clusterstaticpolicies.rightsizing.kubex.ai
    proactivepolicies.rightsizing.kubex.ai
    clusterproactivepolicies.rightsizing.kubex.ai
    rollbackpolicies.rightsizing.kubex.ai
    clusterrollbackpolicies.rightsizing.kubex.ai
    gpurebalancingpolicies.rightsizing.kubex.ai
    clustergpurebalancingpolicies.rightsizing.kubex.ai
    gpuconsolidationpolicies.rightsizing.kubex.ai
    podaffinitypolicies.rightsizing.kubex.ai
  )
  for crd in "${crds[@]}"; do
    wait_for_crd_absent "$crd"
  done
}

ensure_python_env
bootstrap_cluster
run_functional_suite "$@"
if [[ "$KEEP_KIND_CLUSTER" == "1" ]] || [[ "$KEEP_KIND_CLUSTER" == "true" ]]; then
  log "KEEP_KIND_CLUSTER set — skipping uninstall and cluster teardown"
else
  verify_uninstall
fi
