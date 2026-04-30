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
   - Adds a VPA targeting the Deployment and filters CPU resizing once VPA publishes a CPU recommendation.
   - Exercises `enableVpaFilter`.

6) `vpa-filter-default.yaml`
   - Adds a VPA without `resourcePolicy` (VPA defaults to all containers, CPU+memory once recommendations are published).
   - Exercises `enableVpaFilter` with VPA defaults.

7) `vpa-filter-recommendation-managed.yaml`
   - Mirrors the reported `acs-helm-operator` case where VPA `resourcePolicy` constrains only CPU.
   - The pod starts at `40Mi` memory while the StaticPolicy tries to downsize further to `32Mi`; once VPA publishes a memory recommendation, `enableVpaFilter` should block that memory action so VPA can grow memory instead.

8) `limit-range-filter.yaml`
   - Adds a container-scoped `LimitRange` that the policy violates.
   - Exercises `enableLimitRangeFilter`.

9) `pod-limit-range-filter.yaml`
   - Adds a pod-scoped `LimitRange` and a multi-container Deployment.
   - Exercises `enablePodLimitRangeFilter`.

10) `min-change-thresholds.yaml`
   - Sets high minimum change thresholds to filter small CPU/memory adjustments.
   - Exercises `minCpuChangePercent` and `minMemoryChangePercent`.

11) `min-ready-seconds.yaml`
   - Requires the pod to be Ready for a minimum duration before resize.
   - Exercises `minReadyDuration` and `resizeRetryInterval`.

12) `owner-ready-max-unavailable.yaml`
   - Keeps pods unready and sets `maxUnavailable: 0` to block resizing.
   - Exercises `requireOwnerPodsReady` and `respectWorkloadMaxUnavailable`.

13) `node-allocatable-headroom.yaml`
   - Uses large request targets with node headroom applied.
   - Exercises `requireNodeAllocatable`, `nodeCpuHeadroom`, and `nodeMemoryHeadroom`.

14) `per-container-bounds.yaml`
     - Contains the valid `AutomationStrategy` and `ClusterAutomationStrategy` resources.
      - The invalid validation cases live under
        `examples/invalid/automationstrategy/per-container-bounds-validation.yaml`.

14) `scheduling-windows.yaml`
   - Adds inclusion and exclusion windows with UTC and non-UTC IANA timezones.
   - Exercises `spec.scheduling.inclusionWindows` and `spec.scheduling.exclusionWindows`.

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
kubectl apply -f examples/automationstrategy/vpa-filter-recommendation-managed.yaml
kubectl apply -f examples/automationstrategy/limit-range-filter.yaml
kubectl apply -f examples/automationstrategy/pod-limit-range-filter.yaml
kubectl apply -f examples/automationstrategy/min-change-thresholds.yaml
kubectl apply -f examples/automationstrategy/min-ready-seconds.yaml
kubectl apply -f examples/automationstrategy/owner-ready-max-unavailable.yaml
kubectl apply -f examples/automationstrategy/node-allocatable-headroom.yaml
kubectl apply -f examples/automationstrategy/per-container-bounds.yaml
kubectl apply -f examples/automationstrategy/scheduling-windows.yaml
```

Then inspect the matching workloads and policy evaluation results (for example, `kubectl describe policyevaluation` and controller logs) to confirm the expected safety check behavior.

For the validation-focused bundle, `kubectl apply` should create the valid
resources. To preview the invalid cases without persisting changes, use
`kubectl apply --dry-run=server -f examples/invalid/automationstrategy/per-container-bounds-validation.yaml`.

For schedule-driven retries, look for the `scheduling-window-blocked` filter
metadata and `nextAllowedAt` in the controller logs. Admission-time pod
creation ignores schedule windows and may still apply the desired resources
immediately.
