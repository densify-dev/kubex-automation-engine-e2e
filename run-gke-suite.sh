#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"
E2E_DIR="${REPO_ROOT}/test/e2e"

VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"
REQ_FILE="${REQ_FILE:-${REPO_ROOT}/test/e2e/requirements.txt}"
REQ_STAMP="${VENV_DIR}/.requirements.sha256"

PYTEST_BIN="${PYTEST_BIN:-${VENV_PYTHON}}"
PYTEST_WORKERS="${PYTEST_WORKERS:-}"

KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
CLUSTER_NAME="${CLUSTER_NAME:-${KUBE_CONTEXT##*/}}"
HELM_NAMESPACE="${HELM_NAMESPACE:-kubex}"
HELM_RELEASE="${HELM_RELEASE:-kubex-automation-engine}"
HELM_REPO_NAME="${HELM_REPO_NAME:-kubex}"
HELM_REPO_URL="${HELM_REPO_URL:-https://densify-dev.github.io/helm-charts}"
HELM_CRDS_CHART="${HELM_CRDS_CHART:-charts/kubex-crds}"
HELM_CONTROLLER_CHART="${HELM_CONTROLLER_CHART:-charts/kubex-automation-engine}"
HELM_CRDS_CHART_VERSION="${HELM_CRDS_CHART_VERSION:-}"
HELM_CONTROLLER_CHART_VERSION="${HELM_CONTROLLER_CHART_VERSION:-}"

CONTROLLER_IMAGE_REPOSITORY="${CONTROLLER_IMAGE_REPOSITORY:-densify/automation-controller}"
CONTROLLER_IMAGE_TAG="${CONTROLLER_IMAGE_TAG:-1.7.0-beta1}"
CONTROLLER_IMAGE_PULL_POLICY="${CONTROLLER_IMAGE_PULL_POLICY:-IfNotPresent}"
CLEANUP_IMAGE_REPOSITORY="${CLEANUP_IMAGE_REPOSITORY:-densify/kubex-automation-cleanup}"
CLEANUP_IMAGE_TAG="${CLEANUP_IMAGE_TAG:-1.7.0-beta1}"
CLEANUP_IMAGE_PULL_POLICY="${CLEANUP_IMAGE_PULL_POLICY:-IfNotPresent}"

KUBEX_URL_HOST="${KUBEX_URL_HOST:-localhost}"
KUBEX_URL_SCHEME="${KUBEX_URL_SCHEME:-http}"
RECOMMENDATIONS_FILE="${RECOMMENDATIONS_FILE:-${REPO_ROOT}/examples/recommendations.json}"

WITH_METRICS_SERVER="${WITH_METRICS_SERVER:-false}"
WITH_KEDA="${WITH_KEDA:-true}"
WITH_VPA="${WITH_VPA:-true}"
DEPLOY_KUBEX_STUB="${DEPLOY_KUBEX_STUB:-false}"
KEEP_KIND_CLUSTER="${KEEP_KIND_CLUSTER:-true}"

is_true() {
  [[ "$1" == "1" ]] || [[ "$1" == "true" ]]
}

log() {
  echo
  echo "==> $*"
}

run_cmd() {
  echo "+ $*"
  "$@"
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
  fi
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

bootstrap_gke_cluster() {
  local args=(
    --kube-context "$KUBE_CONTEXT"
    --kind-cluster-name "$CLUSTER_NAME"
    --namespace "$HELM_NAMESPACE"
    --helm-release "$HELM_RELEASE"
    --helm-repo-url "$HELM_REPO_URL"
    --helm-crds-chart "$HELM_CRDS_CHART"
    --helm-controller-chart "$HELM_CONTROLLER_CHART"
    --controller-image-repository "$CONTROLLER_IMAGE_REPOSITORY"
    --controller-image-tag "$CONTROLLER_IMAGE_TAG"
    --cleanup-image-repository "$CLEANUP_IMAGE_REPOSITORY"
    --cleanup-image-tag "$CLEANUP_IMAGE_TAG"
    --kubex-url-host "$KUBEX_URL_HOST"
    --kubex-url-scheme "$KUBEX_URL_SCHEME"
    --recommendations-file "$RECOMMENDATIONS_FILE"
    --skip-kind-cluster-create
    --deploy-kubex-stub
  )

  if ! is_true "$WITH_METRICS_SERVER"; then
    args+=(--without-metrics-server)
  fi
  if ! is_true "$WITH_KEDA"; then
    args+=(--without-keda)
  fi
  if ! is_true "$WITH_VPA"; then
    args+=(--without-vpa)
  fi

  log "Bootstrapping controller onto existing GKE cluster ${KUBE_CONTEXT}"
  run_cmd "$VENV_PYTHON" "$REPO_ROOT/test/e2e/bootstrap.py" "${args[@]}"
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
    --controller-image-repository "$CONTROLLER_IMAGE_REPOSITORY"
    --controller-image-tag "$CONTROLLER_IMAGE_TAG"
    --controller-image-pull-policy "$CONTROLLER_IMAGE_PULL_POLICY"
    --test-namespace e2e-test
    --timeout 180
  )
  if [[ -n "$PYTEST_WORKERS" ]]; then
    args+=(-n "$PYTEST_WORKERS")
  fi
  args+=("${pytest_targets[@]}")

  log "Running full e2e suite on ${KUBE_CONTEXT}"
  (cd "$E2E_DIR" && run_cmd "$PYTEST_BIN" -m pytest "${args[@]}")
}

ensure_python_env
bootstrap_gke_cluster
run_functional_suite "$@"
