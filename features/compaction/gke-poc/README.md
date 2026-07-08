# GKE Compaction POC

This is the cheapest proof-of-movement version of the compaction test.
It targets a small `GKE Standard` cluster with `3 x e2-standard-2` nodes.

It has two sub-tests:

- single pass: verify that `candidate-0` moves from the light node to one of the busier nodes
- organic rebound: let the candidate be evicted from the light node and attempt a natural return after busy-node blockers are already active and candidate eligibility is opened in stages

Working POC tuning:

- busy pods: `700m CPU / 1024Mi`
- candidate pod: `100m CPU / 128Mi`
- rebound blockers: `500m CPU / 256Mi`
- descheduler thresholds: `cpu: 60`, `memory: 25`, `pods: 100`

Organic rebound mode:

- run `./test/e2e/features/compaction/gke-poc/run-gke-poc-organic-rebound.sh`
- or set `GKE_POC_MODE=organic-rebound` when invoking `run-gke-poc.sh`
- base busy workloads are allowed to settle first
- blocker pods are pre-created but remain pending until rebound is enabled on the busy nodes
- the candidate is initially eligible only on the light node
- right before the descheduler run, blocker eligibility and candidate eligibility are opened together
- the candidate should be recreated with a new UID and end back on the light node

It does **not** include Prometheus or Grafana.

## Assumptions

- GKE Standard, zonal cluster
- `3` worker nodes, all `e2-standard-2`
- Cluster autoscaler enabled with `optimize-utilization`
- A shell with `gcloud`, `kubectl`, and `helm`

## Create Cluster

You can either run the script end-to-end:

```bash
GKE_PROJECT_ID="<your-project>" \
./test/e2e/features/compaction/gke-poc/run-gke-poc.sh
```

To try the organic rebound path:

```bash
GKE_PROJECT_ID="<your-project>" GKE_POC_MODE=organic-rebound \
./test/e2e/features/compaction/gke-poc/run-gke-poc.sh
```

Or run the steps manually:

```bash
export PROJECT_ID="<your-project>"
export REGION="us-central1"
export CLUSTER_NAME="compaction-poc"

gcloud config set project "${PROJECT_ID}"
gcloud container clusters create "${CLUSTER_NAME}" \
  --zone "${REGION}-b" \
  --machine-type e2-standard-2 \
  --num-nodes 3 \
  --enable-autoscaling \
  --min-nodes 1 \
  --max-nodes 3 \
  --autoscaling-profile optimize-utilization \
  --release-channel regular

gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${REGION}-b"
```

## Label Nodes

```bash
mapfile -t NODES < <(kubectl get nodes -l '!node-role.kubernetes.io/control-plane' \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

LIGHT_NODE="${NODES[0]}"
BUSY_NODE_A="${NODES[1]}"
BUSY_NODE_B="${NODES[2]}"

kubectl label node "${BUSY_NODE_A}" scheduling.kubex.ai/role=busy-a --overwrite
kubectl label node "${BUSY_NODE_B}" scheduling.kubex.ai/role=busy-b --overwrite
kubectl label node "${LIGHT_NODE}" \
  scheduling.kubex.ai/role=light \
  scheduling.kubex.ai/candidate-eligible=true \
  --overwrite
```

## Apply Workloads

```bash
kubectl apply -f test/e2e/features/compaction/gke-poc/workloads.yaml
kubectl wait --for=condition=Ready pod/candidate-0 -n compaction-poc --timeout=180s
kubectl get pod -n compaction-poc candidate-0 -o wide
```

The candidate should land on `LIGHT_NODE` first because it is the only node
eligible for the candidate selector.

## Open Busy Nodes

```bash
kubectl label node "${BUSY_NODE_A}" scheduling.kubex.ai/candidate-eligible=true --overwrite
kubectl label node "${BUSY_NODE_B}" scheduling.kubex.ai/candidate-eligible=true --overwrite
```

## Enable Rebound Blockers

For the current hybrid experiment, enable the blocker nodes only after the base busy workloads are already placed:

```bash
kubectl label node "${BUSY_NODE_A}" scheduling.kubex.ai/rebound-enabled=true --overwrite
kubectl label node "${BUSY_NODE_B}" scheduling.kubex.ai/rebound-enabled=true --overwrite
```

## Install Descheduler

```bash
helm repo add descheduler https://kubernetes-sigs.github.io/descheduler/
helm repo update

helm upgrade --install descheduler descheduler/descheduler \
  -n kube-system \
  -f test/e2e/features/compaction/gke-poc/descheduler-values.yaml

kubectl delete job descheduler-once -n kube-system --ignore-not-found
kubectl create job --from=cronjob/descheduler descheduler-once -n kube-system
kubectl wait --for=condition=complete job/descheduler-once -n kube-system --timeout=180s
kubectl logs -n kube-system job/descheduler-once
```

## Success Criteria

- `candidate-0` starts on the light node
- after the descheduler run, `candidate-0` moves to `BUSY_NODE_A` or
  `BUSY_NODE_B`

For the organic rebound sub-test:

- the descheduler logs show `candidate-0` was evicted
- blocker pods become active only after the base busy workloads have already settled
- `candidate-0` is recreated with a new pod UID
- the final placement is back on `LIGHT_NODE`

## Tried So Far

1. Forced ping-pong reset
   - explicitly deleted the candidate between descheduler runs
   - reproduced repeated movement, but it was not organic
2. Post-eviction blocker scale-up
   - scaled blocker deployments after eviction began
   - failed because busy-node fit was decided before the blockers became effective
3. Post-eviction label-gated blockers
   - pre-created blockers but opened their gate only after eviction began
   - failed for the same reason: descheduler still saw a busy-node fit path first
4. Current try: staged candidate eligibility
   - blockers were active before descheduler evaluation
   - candidate is initially eligible only on the light node
   - busy nodes become candidate-eligible only right before descheduler runs
   - live result: failed early because the pre-enabled blockers prevented the base busy workload from fully coming up
5. Current next try: hybrid gated blockers plus staged candidate eligibility
   - base busy workloads settle first
   - blockers stay pending until rebound is enabled
   - busy nodes become candidate-eligible at the same point blockers are enabled
   - with blockers at `1500m`, this failed before descheduler because the blockers over-constrained a busy node
6. `nodeFit: false` with lower blocker CPU
   - lowered both blockers to `500m`
   - both blockers rolled out successfully
   - descheduler ran and evicted `candidate-0`
   - `candidate-0` rescheduled onto a busy node instead of rebounding to light
   - this is the first clean live run that reached the descheduler step under the rebound variants
7. Diagnostic event capture on the `nodeFit: false` run
   - `rebound-busy-a` preempted the original `busy-a` pod
   - that freed `busy-a` and made it a valid landing spot for the recreated candidate
   - the blocker model was therefore self-defeating
8. Current next try: non-preempting blockers
   - keep blockers at `500m`
   - keep `nodeFit: false`
   - change blockers to `preemptionPolicy: Never`
   - goal: consume spare headroom without displacing the baseline busy workload
9. First successful organic rebound result
   - kept `nodeFit: false`
   - kept blockers at `500m CPU / 256Mi`
   - descheduler evicted `candidate-0`
   - `candidate-0` was recreated with a new UID on the light node
   - this is the first run that achieved the intended organic rebound behavior
   - follow-up: event history still showed a preemption on `busy-a`, so that detail needs inspection

## Next Tries

1. Inspect why the event stream still showed a preemption on `busy-a` during the successful run
2. Decide whether the real target is immediate rebound to light or a repeatable multi-pass eviction loop under repeated descheduler runs
3. If needed, tighten the model so the successful rebound does not depend on side effects we do not yet understand

## If It Fails

If the single-pass candidate stays on the light node, try these in order:

1. increase the busy pod requests slightly
2. lower the descheduler thresholds to `15`
3. move to `e2-standard-4`

If the organic rebound candidate still lands on a busy node, tune the blocker requests first:

1. inspect the successful run's pod and scheduling events before changing resources again
2. if needed, increase one non-preempting blocker CPU request slightly at a time
3. if the blockers stop fitting, move the cluster to `e2-standard-4`

`e2-medium` was tested first and proved too small for this POC.
