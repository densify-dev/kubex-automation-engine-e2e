#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEATURE_ROOT="${SCRIPT_DIR}"

ORIGINAL_KUBE_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"

GKE_PROJECT_ID="${GKE_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
GKE_REGION="${GKE_REGION:-us-central1}"
GKE_ZONE="${GKE_ZONE:-${GKE_REGION}-b}"
GKE_CLUSTER_NAME="${GKE_CLUSTER_NAME:-compaction-poc}"
GKE_MACHINE_TYPE="${GKE_MACHINE_TYPE:-e2-standard-2}"
GKE_NODE_COUNT="${GKE_NODE_COUNT:-3}"
GKE_MIN_NODES="${GKE_MIN_NODES:-1}"
GKE_MAX_NODES="${GKE_MAX_NODES:-3}"
KEEP_GKE_CLUSTER="${KEEP_GKE_CLUSTER:-}"
DESCHEDULER_RELEASE="${DESCHEDULER_RELEASE:-descheduler}"
DESCHEDULER_NAMESPACE="${DESCHEDULER_NAMESPACE:-kube-system}"
MOVE_TIMEOUT_SECONDS="${MOVE_TIMEOUT_SECONDS:-240}"
GKE_POC_MODE="${GKE_POC_MODE:-single-pass}"
DESCHEDULER_LOGS=""

log() {
  echo
  echo "==> $*"
}

run_cmd() {
  echo "+ $*"
  "$@"
}

is_true() {
  [[ "$1" == "1" || "$1" == "true" || "$1" == "TRUE" ]]
}

kubectl_ctx() {
  kubectl "$@"
}

cleanup() {
  local exit_code=$?

  if (( exit_code != 0 )); then
    echo
    echo "==> Failure diagnostics"
    kubectl_ctx get nodes -o wide || true
    kubectl_ctx get pods -A -o wide || true
    kubectl_ctx logs -n "${DESCHEDULER_NAMESPACE}" job/descheduler-once || true
  fi

  if ! is_true "${KEEP_GKE_CLUSTER}"; then
    if gcloud container clusters describe "${GKE_CLUSTER_NAME}" --zone "${GKE_ZONE}" >/dev/null 2>&1; then
      run_cmd gcloud container clusters delete "${GKE_CLUSTER_NAME}" --zone "${GKE_ZONE}" --quiet
    fi
  fi

  if [[ -n "${ORIGINAL_KUBE_CONTEXT}" ]]; then
    kubectl config use-context "${ORIGINAL_KUBE_CONTEXT}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_for_job_complete() {
  kubectl_ctx wait --for=condition=complete "job/descheduler-once" -n "${DESCHEDULER_NAMESPACE}" --timeout=180s
}

wait_for_pod_node_change() {
  local namespace="$1"
  local pod_name="$2"
  local old_node="$3"
  local deadline=$((SECONDS + MOVE_TIMEOUT_SECONDS))

  while (( SECONDS < deadline )); do
    local node
    node="$(kubectl_ctx get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)"
    if [[ -n "${node}" && "${node}" != "${old_node}" ]]; then
      printf '%s\n' "${node}"
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for ${pod_name} to move away from ${old_node}" >&2
  return 1
}

wait_for_pod_on_node() {
  local namespace="$1"
  local pod_name="$2"
  local expected_node="$3"
  local deadline=$((SECONDS + MOVE_TIMEOUT_SECONDS))

  while (( SECONDS < deadline )); do
    local node
    node="$(kubectl_ctx get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)"
    if [[ "${node}" == "${expected_node}" ]]; then
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for ${pod_name} to land on ${expected_node}" >&2
  return 1
}

wait_for_pod_uid_change() {
  local namespace="$1"
  local pod_name="$2"
  local old_uid="$3"
  local deadline=$((SECONDS + MOVE_TIMEOUT_SECONDS))

  while (( SECONDS < deadline )); do
    local uid
    uid="$(kubectl_ctx get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
    if [[ -n "${uid}" && "${uid}" != "${old_uid}" ]]; then
      printf '%s\n' "${uid}"
      return 0
    fi
    sleep 1
  done

  echo "Timed out waiting for ${pod_name} UID to change from ${old_uid}" >&2
  return 1
}

create_cluster() {
  if gcloud container clusters describe "${GKE_CLUSTER_NAME}" --zone "${GKE_ZONE}" >/dev/null 2>&1; then
    run_cmd gcloud container clusters delete "${GKE_CLUSTER_NAME}" --zone "${GKE_ZONE}" --quiet
  fi

  run_cmd gcloud container clusters create "${GKE_CLUSTER_NAME}" \
    --zone "${GKE_ZONE}" \
    --machine-type "${GKE_MACHINE_TYPE}" \
    --num-nodes "${GKE_NODE_COUNT}" \
    --enable-autoscaling \
    --min-nodes "${GKE_MIN_NODES}" \
    --max-nodes "${GKE_MAX_NODES}" \
    --autoscaling-profile optimize-utilization \
    --release-channel regular

  run_cmd gcloud container clusters get-credentials "${GKE_CLUSTER_NAME}" --zone "${GKE_ZONE}"
  run_cmd kubectl wait --for=condition=Ready nodes --all --timeout=300s
}

label_nodes() {
  mapfile -t NODES < <(kubectl_ctx get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

  if [[ "${#NODES[@]}" -lt 3 ]]; then
    echo "Expected at least 3 nodes, found ${#NODES[@]}" >&2
    exit 1
  fi

  LIGHT_NODE="${NODES[0]}"
  BUSY_NODE_A="${NODES[1]}"
  BUSY_NODE_B="${NODES[2]}"

  run_cmd kubectl_ctx label node "${BUSY_NODE_A}" scheduling.kubex.ai/role=busy-a --overwrite
  run_cmd kubectl_ctx label node "${BUSY_NODE_B}" scheduling.kubex.ai/role=busy-b --overwrite
  run_cmd kubectl_ctx label node "${LIGHT_NODE}" \
    scheduling.kubex.ai/role=light \
    scheduling.kubex.ai/candidate-eligible=true \
    --overwrite
}

apply_workloads() {
  run_cmd kubectl_ctx apply -f "${FEATURE_ROOT}/workloads.yaml"
  run_cmd kubectl_ctx rollout status deploy/busy-a -n compaction-poc --timeout=180s
  run_cmd kubectl_ctx rollout status deploy/busy-b -n compaction-poc --timeout=180s
  run_cmd kubectl_ctx wait --for=condition=Ready pod/candidate-0 -n compaction-poc --timeout=180s
  kubectl_ctx get pod -n compaction-poc candidate-0 -o wide
  wait_for_pod_on_node compaction-poc candidate-0 "${LIGHT_NODE}"
}

open_busy_nodes() {
  run_cmd kubectl_ctx label node "${BUSY_NODE_A}" scheduling.kubex.ai/candidate-eligible=true --overwrite
  run_cmd kubectl_ctx label node "${BUSY_NODE_B}" scheduling.kubex.ai/candidate-eligible=true --overwrite
}

enable_rebound_blockers() {
  log "Enabling rebound blockers on busy nodes"
  run_cmd kubectl_ctx label node "${BUSY_NODE_A}" scheduling.kubex.ai/rebound-enabled=true --overwrite
  run_cmd kubectl_ctx label node "${BUSY_NODE_B}" scheduling.kubex.ai/rebound-enabled=true --overwrite
}

wait_for_rebound_blockers() {
  log "Waiting for rebound blockers to become active"
  run_cmd kubectl_ctx rollout status deploy/rebound-busy-a -n compaction-poc --timeout=180s
  run_cmd kubectl_ctx rollout status deploy/rebound-busy-b -n compaction-poc --timeout=180s
}

install_descheduler() {
  run_cmd helm repo add descheduler https://kubernetes-sigs.github.io/descheduler/
  run_cmd helm repo update
  run_cmd helm upgrade --install "${DESCHEDULER_RELEASE}" descheduler/descheduler \
    -n "${DESCHEDULER_NAMESPACE}" \
    -f "${FEATURE_ROOT}/descheduler-values.yaml"
}

run_descheduler_once() {
  run_cmd kubectl_ctx delete job descheduler-once -n "${DESCHEDULER_NAMESPACE}" --ignore-not-found
  run_cmd kubectl_ctx create job --from=cronjob/"${DESCHEDULER_RELEASE}" descheduler-once -n "${DESCHEDULER_NAMESPACE}"
  wait_for_job_complete
  DESCHEDULER_LOGS="$(kubectl_ctx logs -n "${DESCHEDULER_NAMESPACE}" job/descheduler-once || true)"
  printf '%s\n' "${DESCHEDULER_LOGS}"
}

verify_move() {
  local new_node
  new_node="$(wait_for_pod_node_change compaction-poc candidate-0 "${LIGHT_NODE}")"

  if [[ "${new_node}" != "${BUSY_NODE_A}" && "${new_node}" != "${BUSY_NODE_B}" ]]; then
    echo "candidate-0 moved to unexpected node ${new_node}" >&2
    exit 1
  fi

  log "Compaction succeeded"
  echo "candidate-0 moved from ${LIGHT_NODE} to ${new_node}"
  kubectl_ctx get pods -n compaction-poc -o wide
  kubectl_ctx get nodes
}

verify_organic_rebound() {
  local initial_uid="$1"
  local rebound_uid
  rebound_uid="$(wait_for_pod_uid_change compaction-poc candidate-0 "${initial_uid}")"

  run_cmd kubectl_ctx wait --for=condition=Ready pod/candidate-0 -n compaction-poc --timeout=180s
  wait_for_pod_on_node compaction-poc candidate-0 "${LIGHT_NODE}"

  if [[ "${DESCHEDULER_LOGS}" != *'Evicted pod'* || "${DESCHEDULER_LOGS}" != *'compaction-poc/candidate-0'* ]]; then
    echo "descheduler logs did not show candidate-0 eviction" >&2
    exit 1
  fi

  log "Organic rebound succeeded"
  echo "candidate-0 was evicted and recreated with UID ${rebound_uid} on ${LIGHT_NODE}"
  kubectl_ctx get pods -n compaction-poc -o wide
  kubectl_ctx get nodes
}

if [[ -z "${GKE_PROJECT_ID}" ]]; then
  echo "GKE_PROJECT_ID must be set or configured in gcloud" >&2
  exit 1
fi

log "Using project ${GKE_PROJECT_ID}, cluster ${GKE_CLUSTER_NAME}, zone ${GKE_ZONE}"

run_cmd gcloud config set project "${GKE_PROJECT_ID}"
create_cluster
label_nodes
apply_workloads
install_descheduler

case "${GKE_POC_MODE}" in
  single-pass)
    log "Starting single-pass compaction run"
    open_busy_nodes
    run_descheduler_once
    verify_move
    ;;
  organic-rebound)
    log "Starting organic rebound compaction run"
    CANDIDATE_UID="$(kubectl_ctx get pod candidate-0 -n compaction-poc -o jsonpath='{.metadata.uid}')"
    enable_rebound_blockers
    open_busy_nodes
    wait_for_rebound_blockers
    run_descheduler_once
    verify_organic_rebound "${CANDIDATE_UID}"
    ;;
  *)
    echo "Unknown GKE_POC_MODE: ${GKE_POC_MODE}" >&2
    echo "Expected one of: single-pass, organic-rebound" >&2
    exit 1
    ;;
esac
