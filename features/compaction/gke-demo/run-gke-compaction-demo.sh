#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEATURE_ROOT="${SCRIPT_DIR}"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../.." && pwd)"

GKE_PROJECT_ID="${GKE_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
GKE_REGION="${GKE_REGION:-us-central1}"
GKE_ZONE="${GKE_ZONE:-${GKE_REGION}-b}"
GKE_CLUSTER_NAME="${GKE_CLUSTER_NAME:-compaction-demo}"
GKE_MACHINE_TYPE="${GKE_MACHINE_TYPE:-e2-standard-2}"
GKE_NODE_COUNT="${GKE_NODE_COUNT:-3}"
GKE_MIN_NODES="${GKE_MIN_NODES:-1}"
GKE_MAX_NODES="${GKE_MAX_NODES:-3}"
KEEP_GKE_CLUSTER="${KEEP_GKE_CLUSTER:-true}"

CONTROLLER_IMAGE_REPOSITORY="${CONTROLLER_IMAGE_REPOSITORY:-densify/automation-controller}"
CONTROLLER_IMAGE_TAG="${CONTROLLER_IMAGE_TAG:-1.7.0-beta1}"
CLEANUP_IMAGE_REPOSITORY="${CLEANUP_IMAGE_REPOSITORY:-densify/kubex-automation-cleanup}"
CLEANUP_IMAGE_TAG="${CLEANUP_IMAGE_TAG:-1.7.0-beta1}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
GRAFANA_PORT_FORWARD_PORT="${GRAFANA_PORT_FORWARD_PORT:-3000}"

KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context 2>/dev/null || true)}"

log() {
  echo
  echo "==> $*"
}

NODE_LIGHT=""
NODE_BUSY_A=""
NODE_BUSY_B=""

run_cmd() {
  echo "+ $*"
  "$@"
}

bootstrap_monitoring() {
  run_cmd kubectl apply -f "${REPO_ROOT}/config/monitoring/namespace.yaml"
  run_cmd helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server >/dev/null
  run_cmd helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
  run_cmd helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
  run_cmd kubectl apply -f "${REPO_ROOT}/config/monitoring/prometheus-config.yaml"
  run_cmd kubectl apply -f "${REPO_ROOT}/config/monitoring/prometheus-deploy.yaml"
  run_cmd kubectl apply -f "${REPO_ROOT}/config/monitoring/grafana-config.yaml"
  run_cmd kubectl apply -f "${REPO_ROOT}/config/monitoring/grafana-dashboards.yaml"
  run_cmd kubectl apply -f "${REPO_ROOT}/config/monitoring/grafana-deploy.yaml"
  run_cmd helm upgrade --install kube-state-metrics prometheus-community/kube-state-metrics -n monitoring --create-namespace --wait
}

start_grafana_port_forward() {
  local port_forward_log
  port_forward_log="${FEATURE_ROOT}/grafana-port-forward.log"
  run_cmd kubectl -n monitoring port-forward svc/grafana "${GRAFANA_PORT_FORWARD_PORT}:3000" >"${port_forward_log}" 2>&1 &
  echo $!
}

print_port_forward_instructions() {
  echo "Grafana: http://127.0.0.1:${GRAFANA_PORT_FORWARD_PORT}"
  echo "Grafana port-forward log: ${FEATURE_ROOT}/grafana-port-forward.log"
}

bootstrap_controller() {
  kubectl get namespace kubex >/dev/null 2>&1 || run_cmd kubectl create namespace kubex
  kubectl -n kubex get configmap recommendations >/dev/null 2>&1 || run_cmd kubectl -n kubex create configmap recommendations \
    --from-file=recommendations.json="${REPO_ROOT}/examples/recommendations.json"
  run_cmd helm upgrade --install kubex-crds "${REPO_ROOT}/charts/kubex-crds" \
    --namespace kubex --create-namespace --wait
  run_cmd helm upgrade --install kubex-automation-engine "${REPO_ROOT}/charts/kubex-automation-engine" \
    --namespace kubex --create-namespace --wait --timeout 10m \
    --set createSecrets=true \
    --set kubex.url.host=localhost \
    --set kubex.url.scheme=http \
    --set kubex.clusterName="${GKE_CLUSTER_NAME}" \
    --set kubexCredentials.username=dummy \
    --set kubexCredentials.epassword=dummy \
    --set webhook.certManager.enabled=false \
    --set gateway.enabled=false \
    --set localRecommendations.enabled=true \
    --set localRecommendations.configMapName=recommendations \
    --set globalConfiguration.suppressFetchRecommendations=true \
    --set compactionScheduler.enabled=true \
    --set compactionDescheduler.enabled=true \
    --set compactionDescheduler.interval=1m \
    --set compactionDescheduler.suspend=false \
    --set compactionDescheduler.schedule='*/1 * * * *' \
    --set image.repository="${CONTROLLER_IMAGE_REPOSITORY}" \
    --set image.tag="${CONTROLLER_IMAGE_TAG}" \
    --set cleanup.image.repository="${CLEANUP_IMAGE_REPOSITORY}" \
    --set cleanup.image.tag="${CLEANUP_IMAGE_TAG}"
}

bootstrap_demo_workloads() {
  kubectl get namespace compaction-demo >/dev/null 2>&1 || run_cmd kubectl create namespace compaction-demo
  kubectl -n compaction-demo delete deployment busy-a busy-b spare-a spare-b candidate --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n compaction-demo delete svc candidate --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n compaction-demo delete statefulset candidate --ignore-not-found >/dev/null 2>&1 || true
  cat <<'EOF' | kubectl apply -f -
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: compaction-demo-high
value: 1000000
preemptionPolicy: Never
globalDefault: false
description: Non-preempting pods used to keep the light node empty after compaction.
EOF
  cat <<'EOF' | kubectl apply -f -
apiVersion: rightsizing.kubex.ai/v1alpha1
kind: ClusterCompactionPolicy
metadata:
  name: compaction-demo
spec:
  scope:
    labelSelector:
      matchLabels:
        team: platform
    workloadTypes:
      - Deployment
      - StatefulSet
    namespaceSelector:
      operator: NotIn
      values:
        - kube-system
        - kubex
        - monitoring
        - keda
  enabled: true
  scheduler:
    useKubexScheduler: true
    externalSchedulerName: ""
  descheduler:
    enabled: true
    maxNoOfPodsToEvictPerNode: 1
    maxNoOfPodsToEvictPerNamespace: 1
    maxNoOfPodsToEvictTotal: 1
    defaultEvictor:
      nodeFit: true
      evictSystemCriticalPods: false
      evictLocalStoragePods: false
      ignorePvcPods: true
      evictDaemonSetPods: false
      labelSelector:
        matchLabels:
          scheduling.kubex.ai/binpack: "true"
    highNodeUtilization:
      numberOfNodes: 0
      thresholds:
        cpu: 40
        memory: 20
        pods: 10
  weight: 50
EOF
  cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Namespace
metadata:
  name: compaction-demo
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: busy-a
  namespace: compaction-demo
  labels:
    team: platform
spec:
  replicas: 1
  selector:
    matchLabels:
      app: busy-a
      team: platform
  template:
    metadata:
      labels:
        app: busy-a
        team: platform
        scheduling.kubex.ai/binpack: "true"
        scheduling.kubex.ai/compaction-policy: compaction-demo
    spec:
      nodeSelector:
        scheduling.kubex.ai/role: busy
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests:
              cpu: "300m"
              memory: "512Mi"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: busy-b
  namespace: compaction-demo
  labels:
    team: platform
spec:
  replicas: 1
  selector:
    matchLabels:
      app: busy-b
      team: platform
  template:
    metadata:
      labels:
        app: busy-b
        team: platform
        scheduling.kubex.ai/binpack: "true"
        scheduling.kubex.ai/compaction-policy: compaction-demo
    spec:
      nodeSelector:
        scheduling.kubex.ai/role: busy
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests:
              cpu: "300m"
              memory: "512Mi"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: spare-a
  namespace: compaction-demo
  labels:
    team: platform
spec:
  replicas: 1
  selector:
    matchLabels:
      app: spare-a
      team: platform
  template:
    metadata:
      labels:
        app: spare-a
        team: platform
        scheduling.kubex.ai/binpack: "true"
        scheduling.kubex.ai/compaction-policy: compaction-demo
    spec:
      priorityClassName: compaction-demo-high
      nodeSelector:
        scheduling.kubex.ai/role: busy
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: spare-b
  namespace: compaction-demo
  labels:
    team: platform
spec:
  replicas: 1
  selector:
    matchLabels:
      app: spare-b
      team: platform
  template:
    metadata:
      labels:
        app: spare-b
        team: platform
        scheduling.kubex.ai/binpack: "true"
        scheduling.kubex.ai/compaction-policy: compaction-demo
    spec:
      priorityClassName: compaction-demo-high
      nodeSelector:
        scheduling.kubex.ai/role: busy
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
---
apiVersion: v1
kind: Service
metadata:
  name: candidate
  namespace: compaction-demo
spec:
  clusterIP: None
  selector:
    app: candidate
    team: platform
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: candidate
  namespace: compaction-demo
  labels:
    team: platform
spec:
  serviceName: candidate
  replicas: 1
  updateStrategy:
    type: OnDelete
  selector:
    matchLabels:
      app: candidate
      team: platform
  template:
    metadata:
      labels:
        app: candidate
        team: platform
        scheduling.kubex.ai/binpack: "true"
        scheduling.kubex.ai/compaction-policy: compaction-demo
    spec:
      nodeSelector:
        scheduling.kubex.ai/candidate-eligible: "true"
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests:
              cpu: "50m"
              memory: "64Mi"
EOF
}

label_nodes() {
  mapfile -t nodes < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
  if [[ ${#nodes[@]} -lt 3 ]]; then
    echo "Need 3 nodes for the demo" >&2
    exit 1
  fi
  NODE_LIGHT="${nodes[0]}"
  NODE_BUSY_A="${nodes[1]}"
  NODE_BUSY_B="${nodes[2]}"
  run_cmd kubectl label node "${NODE_LIGHT}" scheduling.kubex.ai/role=light scheduling.kubex.ai/candidate-eligible=true --overwrite
  run_cmd kubectl label node "${NODE_BUSY_A}" scheduling.kubex.ai/role=busy scheduling.kubex.ai/role-name=busy-a --overwrite
  run_cmd kubectl label node "${NODE_BUSY_B}" scheduling.kubex.ai/role=busy scheduling.kubex.ai/role-name=busy-b --overwrite
}

open_busy_nodes() {
  run_cmd kubectl label node "${NODE_LIGHT}" scheduling.kubex.ai/candidate-eligible- --overwrite
  run_cmd kubectl label node "${NODE_BUSY_A}" scheduling.kubex.ai/candidate-eligible=true --overwrite
  run_cmd kubectl label node "${NODE_BUSY_B}" scheduling.kubex.ai/candidate-eligible=true --overwrite
}

wait_for_demo_workloads() {
  run_cmd kubectl -n compaction-demo rollout status deploy/busy-a --timeout=180s
  run_cmd kubectl -n compaction-demo rollout status deploy/busy-b --timeout=180s
  run_cmd kubectl -n compaction-demo rollout status deploy/spare-a --timeout=180s
  run_cmd kubectl -n compaction-demo rollout status deploy/spare-b --timeout=180s
  run_cmd kubectl -n compaction-demo wait --for=condition=Ready pod/candidate-0 --timeout=180s
  run_cmd kubectl -n compaction-demo get pod candidate-0 -o wide
}

wait_for_grafana() {
  run_cmd kubectl -n monitoring rollout status deploy/grafana --timeout=180s
  run_cmd kubectl -n monitoring rollout status deploy/prometheus --timeout=180s
}

if [[ -z "${GKE_PROJECT_ID}" ]]; then
  echo "GKE_PROJECT_ID must be set or configured in gcloud" >&2
  exit 1
fi

log "Using project ${GKE_PROJECT_ID}, cluster ${GKE_CLUSTER_NAME}, zone ${GKE_ZONE}"
if gcloud container clusters describe "${GKE_CLUSTER_NAME}" --zone "${GKE_ZONE}" --project "${GKE_PROJECT_ID}" >/dev/null 2>&1; then
  log "Reusing existing GKE cluster ${GKE_CLUSTER_NAME}"
else
  run_cmd gcloud container clusters create "${GKE_CLUSTER_NAME}" \
    --zone "${GKE_ZONE}" \
    --project "${GKE_PROJECT_ID}" \
    --machine-type "${GKE_MACHINE_TYPE}" \
    --num-nodes "${GKE_NODE_COUNT}" \
    --enable-autoscaling \
    --min-nodes "${GKE_MIN_NODES}" \
    --max-nodes "${GKE_MAX_NODES}" \
    --autoscaling-profile optimize-utilization \
    --release-channel regular
fi
run_cmd gcloud container clusters get-credentials "${GKE_CLUSTER_NAME}" --zone "${GKE_ZONE}" --project "${GKE_PROJECT_ID}"
run_cmd kubectl wait --for=condition=Ready nodes --all --timeout=600s
label_nodes
bootstrap_monitoring
bootstrap_controller
wait_for_grafana
bootstrap_demo_workloads
wait_for_demo_workloads
open_busy_nodes
start_grafana_port_forward

log "Done"
print_port_forward_instructions
echo "Prometheus: kubectl -n monitoring port-forward svc/prometheus 9090:9090"
echo "Demo workload namespace: compaction-demo"
