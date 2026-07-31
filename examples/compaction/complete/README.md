# Cluster compaction test manifests

Two files, independently applicable:

- `deployments.yaml` creates the `compaction-test` namespace and two Deployments labelled `team: compaction-test`. Soft hostname topology constraints encourage initial spreading without blocking later compaction.
- `compaction-policy.yaml` selects only Deployments with `team: compaction-test` in the `compaction-test` namespace.

The policy runs every minute and uses 70% CPU, memory, and pod thresholds only to make this small demo observable. These aggressive test settings are not production recommendations. CPU and memory utilization are calculated from Pod resource requests, not live usage.

Use a cluster with at least three worker nodes so compaction has enough placement options to work efficiently.

First, deploy the workloads without the compaction policy:

```bash
kubectl apply -f examples/compaction/complete/deployments.yaml
kubectl rollout status deployment -n compaction-test --all
kubectl get pods -n compaction-test -o wide
```

The default scheduler should initially spread the Pods when capacity allows. Because the topology constraint is `ScheduleAnyway`, exact placement is not guaranteed. Note the nodes shown in the `NODE` column.

Next, deploy the compaction policy:

```bash
kubectl apply -f examples/compaction/complete/compaction-policy.yaml
kubectl get deployments -n compaction-test -l scheduling.kubex.ai/compaction-policy=compaction-test
kubectl get clustercompactionpolicy compaction-test -o yaml
```

The policy assigns matching workloads to the Kubex scheduler and replaces existing Pods as needed to converge scheduling intent. Scheduled descheduler runs then evict eligible Pods from underutilized nodes; replacements should move toward fewer nodes when requests, capacity, and other scheduling constraints permit:

```bash
kubectl get pods -n compaction-test -o wide --watch
```

Cleanup separately:

```bash
kubectl delete -f examples/compaction/complete/compaction-policy.yaml
kubectl delete -f examples/compaction/complete/deployments.yaml
```
