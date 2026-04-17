# AutomationStrategy safety check examples

This directory contains example bundles that focus on `AutomationStrategySafetyChecks` using StaticPolicy.

1) `pause-until-annotation.yaml`
   - Uses the `rightsizing.kubex.ai/pause-until` annotation to block resizing.
   - Exercises `enablePauseUntilAnnotationCheck` and a custom `resizeRetryInterval`.

2) `resource-quota.yaml`
   - Adds a namespace `ResourceQuota` that blocks the desired resize.
   - Exercises `enableResourceQuotaFilter`.

3) `hpa-filter.yaml`
   - Adds an HPA targeting the Deployment and filters CPU resizing.
   - Exercises `enableHpaFilter`.

4) `hpa-filter-container.yaml`
   - Adds a container-scoped HPA metric (CPU on a single container).
   - Exercises container-level `enableHpaFilter`.

5) `vpa-filter.yaml`
   - Adds a VPA targeting the Deployment and filters CPU resizing.
   - Exercises `enableVpaFilter`.

6) `vpa-filter-default.yaml`
   - Adds a VPA without `resourcePolicy` (defaults to all containers, CPU+memory).
   - Exercises `enableVpaFilter` with VPA defaults.

7) `limit-range-filter.yaml`
   - Adds a container-scoped `LimitRange` that the policy violates.
   - Exercises `enableLimitRangeFilter`.

8) `pod-limit-range-filter.yaml`
   - Adds a pod-scoped `LimitRange` and a multi-container Deployment.
   - Exercises `enablePodLimitRangeFilter`.

9) `min-change-thresholds.yaml`
   - Sets high minimum change thresholds to filter small CPU/memory adjustments.
   - Exercises `minCpuChangePercent` and `minMemoryChangePercent`.

10) `min-ready-seconds.yaml`
   - Requires the pod to be Ready for a minimum duration before resize.
   - Exercises `minReadyDuration` and `resizeRetryInterval`.

11) `owner-ready-max-unavailable.yaml`
   - Keeps pods unready and sets `maxUnavailable: 0` to block resizing.
   - Exercises `requireOwnerPodsReady` and `respectWorkloadMaxUnavailable`.

12) `node-allocatable-headroom.yaml`
   - Uses large request targets with node headroom applied.
   - Exercises `requireNodeAllocatable`, `nodeCpuHeadroom`, and `nodeMemoryHeadroom`.

13) `per-container-bounds-validation.yaml`
   - Contains valid and intentionally invalid `AutomationStrategy` and `ClusterAutomationStrategy` resources.
   - Exercises admission validation for direct and inherited per-container `floor`/`ceiling` ranges.

Bounds can also be configured under `spec.enablement.*.(requests|limits).floor|ceiling` to clamp desired values during runtime resize planning.

For multi-container workloads, add `spec.enablement.*.(requests|limits).containers.<name>.floor|ceiling` to override those bounds per container while keeping the usage-level values as the fallback defaults.

Apply examples:

```sh
kubectl apply -f examples/automationstrategy/pause-until-annotation.yaml
kubectl apply -f examples/automationstrategy/resource-quota.yaml
kubectl apply -f examples/automationstrategy/hpa-filter.yaml
kubectl apply -f examples/automationstrategy/hpa-filter-container.yaml
kubectl apply -f examples/automationstrategy/vpa-filter.yaml
kubectl apply -f examples/automationstrategy/vpa-filter-default.yaml
kubectl apply -f examples/automationstrategy/limit-range-filter.yaml
kubectl apply -f examples/automationstrategy/pod-limit-range-filter.yaml
kubectl apply -f examples/automationstrategy/min-change-thresholds.yaml
kubectl apply -f examples/automationstrategy/min-ready-seconds.yaml
kubectl apply -f examples/automationstrategy/owner-ready-max-unavailable.yaml
kubectl apply -f examples/automationstrategy/node-allocatable-headroom.yaml
kubectl apply -f examples/automationstrategy/per-container-bounds-validation.yaml
```

Then inspect the matching workloads and policy evaluation results (for example, `kubectl describe policyevaluation` and controller logs) to confirm the expected safety check behavior.

For the validation-focused bundle, `kubectl apply` should create the valid resources and reject the invalid ones with admission errors. To preview that behavior without persisting changes, use `kubectl apply --dry-run=server -f examples/automationstrategy/per-container-bounds-validation.yaml`.
