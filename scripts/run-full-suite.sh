#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"
REQ_FILE="${REQ_FILE:-${REPO_ROOT}/requirements.txt}"
REQ_STAMP="${VENV_DIR}/.requirements.sha256"

PYTEST_BIN="${PYTEST_BIN:-${VENV_PYTHON} -m pytest}"
PYTEST_WORKERS="${PYTEST_WORKERS:-}"

CLUSTER_NAME="${CLUSTER_NAME:-e2e}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
NODE_IMAGE="${NODE_IMAGE:-}"

CONTROLLER_IMAGE_REPOSITORY="${CONTROLLER_IMAGE_REPOSITORY:-}"
CONTROLLER_IMAGE_TAG="${CONTROLLER_IMAGE_TAG:-}"
CONTROLLER_IMAGE_PULL_POLICY="${CONTROLLER_IMAGE_PULL_POLICY:-IfNotPresent}"
RECOMMENDATIONS_FILE="${RECOMMENDATIONS_FILE:-${REPO_ROOT}/examples/recommendations.json}"

HELM_RELEASE="${HELM_RELEASE:-kubex-automation-engine}"
HELM_NAMESPACE="${HELM_NAMESPACE:-kubex}"

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
  else
    log "Python dependencies already match ${REQ_FILE}"
  fi
}

wait_for_release_absent() {
  local release="$1"
  until ! helm status "$release" --kube-context "$KUBE_CONTEXT" --namespace "$HELM_NAMESPACE" >/dev/null 2>&1; do
    sleep 2
  done
}

wait_for_controller_pods_absent() {
  until [ -z "$(kubectl --context "$KUBE_CONTEXT" -n "$HELM_NAMESPACE" get pods -l control-plane=controller-manager -o name 2>/dev/null)" ]; do
    sleep 2
  done
}

wait_for_crd_absent() {
  local crd_name="$1"
  until ! kubectl --context "$KUBE_CONTEXT" get crd "$crd_name" >/dev/null 2>&1; do
    sleep 2
  done
}

bootstrap_cluster() {
  local bootstrap_args=(
    --kube-context "$KUBE_CONTEXT"
    --kind-cluster-name "$CLUSTER_NAME"
    --namespace "$HELM_NAMESPACE"
    --helm-release "$HELM_RELEASE"
  )
  if [[ -n "$CONTROLLER_IMAGE_REPOSITORY" ]]; then
    bootstrap_args+=(--controller-image-repository "$CONTROLLER_IMAGE_REPOSITORY")
  fi
  if [[ -n "$CONTROLLER_IMAGE_TAG" ]]; then
    bootstrap_args+=(--controller-image-tag "$CONTROLLER_IMAGE_TAG")
  fi
  if [[ -n "$NODE_IMAGE" ]]; then
    bootstrap_args+=(--kind-node-image "$NODE_IMAGE")
  fi
  if [[ -n "$RECOMMENDATIONS_FILE" ]]; then
    bootstrap_args+=(--recommendations-file "$RECOMMENDATIONS_FILE")
  fi

  log "Bootstrapping cluster ${KUBE_CONTEXT}"
  run_cmd "$VENV_PYTHON" -m bootstrap "${bootstrap_args[@]}"
}

run_functional_suite() {
  local args=(
    tests/
    -v
    --skip-kind-bootstrap
    --kube-context "$KUBE_CONTEXT"
    --kind-cluster-name "$CLUSTER_NAME"
    --namespace "$HELM_NAMESPACE"
    --helm-release "$HELM_RELEASE"
    --controller-image-pull-policy "$CONTROLLER_IMAGE_PULL_POLICY"
    --test-namespace e2e-test
    --timeout 120
  )
  if [[ -n "$PYTEST_WORKERS" ]]; then
    args+=(-n "$PYTEST_WORKERS")
  fi
  if [[ -n "$CONTROLLER_IMAGE_REPOSITORY" ]]; then
    args+=(--controller-image-repository "$CONTROLLER_IMAGE_REPOSITORY")
  fi
  if [[ -n "$CONTROLLER_IMAGE_TAG" ]]; then
    args+=(--controller-image-tag "$CONTROLLER_IMAGE_TAG")
  fi
  if [[ -n "$NODE_IMAGE" ]]; then
    args+=(--kind-node-image "$NODE_IMAGE")
  fi
  if [[ -n "$RECOMMENDATIONS_FILE" ]]; then
    args+=(--recommendations-file "$RECOMMENDATIONS_FILE")
  fi

  log "Running functional suite on ${KUBE_CONTEXT}"
  echo "+ ${PYTEST_BIN} $(printf '%q ' "${args[@]}")"
  bash -lc "${PYTEST_BIN} $(printf '%q ' "${args[@]}")"
}

verify_uninstall() {
  log "Uninstalling controller Helm release ${HELM_RELEASE}"
  run_cmd helm uninstall "$HELM_RELEASE" --kube-context "$KUBE_CONTEXT" --namespace "$HELM_NAMESPACE"

  log "Verifying controller release removal"
  wait_for_release_absent "$HELM_RELEASE"
  wait_for_controller_pods_absent

  log "Uninstalling CRD Helm release kubex-crds"
  run_cmd helm uninstall kubex-crds --kube-context "$KUBE_CONTEXT" --namespace "$HELM_NAMESPACE"

  log "Verifying CRD removal"
  wait_for_crd_absent automationstrategies.rightsizing.kubex.ai
}

ensure_python_env
bootstrap_cluster
run_functional_suite
verify_uninstall
