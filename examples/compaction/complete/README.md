# Cluster compaction test manifests

Two files, independently applicable:

- `deployments.yaml` creates `compaction-test` namespace and two matching Deployments. Each Deployment spreads replicas evenly across nodes using hostname topology constraints.
- `compaction-policy.yaml` creates cluster-scoped policy matching Deployments with `team: compaction-test` in that namespace.

Use a cluster with at least three worker nodes so compaction has enough placement options to work efficiently.

First, deploy the workloads without the compaction policy:

```bash
kubectl apply -f examples/compaction/complete/deployments.yaml
kubectl rollout status deployment -n compaction-test --all
kubectl get pods -n compaction-test -o wide
```

The pods should initially be spread evenly across nodes. Note the nodes shown in the `NODE` column.

Next, deploy the compaction policy:

```bash
kubectl apply -f examples/compaction/complete/compaction-policy.yaml
kubectl get deployments -n compaction-test -l scheduling.kubex.ai/compaction-policy=compaction-test
kubectl get clustercompactionpolicy compaction-test -o yaml
```

The policy starts rescheduling the matching workloads onto fewer nodes. Watch pod placement until compaction completes:

```bash
kubectl get pods -n compaction-test -o wide --watch
```

Cleanup separately:

```bash
kubectl delete -f examples/compaction/complete/compaction-policy.yaml
kubectl delete -f examples/compaction/complete/deployments.yaml
```
