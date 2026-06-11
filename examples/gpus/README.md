# GPU optimization examples

This directory contains examples for GPU-aware rightsizing.

All GPU/KAI fields and custom resources are experimental and may change. Every manifest in this directory sets `spec.experimental.gpuKaiContract: v1alpha1-2026-04` where required.

1) `simple-static-gpu-vanilla.yaml`
   - Namespaced `AutomationStrategy` + `StaticPolicy` using `gpu` under `resources.containers["*"].requests`.
   - Workload starts with `nvidia.com/gpu` requests/limits and no scheduler override.

2) `simple-static-gpu-kai.yaml`
   - Demonstrates GPU override scheduling to KAI with `spec.enablement.overrideScheduler: "kai"`.
   - Includes a `kai.scheduler/queue` pod label and `gpu-fraction` annotation.
   - Uses `kai.setQueueWhenSpecified: false` to preserve an existing queue label.

3) `simple-static-gpu-vanilla-2kai.yaml`
   - Starts with a vanilla workload using `nvidia.com/gpu` requests/limits.
   - Demonstrates migration behavior by enabling `spec.enablement.overrideScheduler: "kai"` in strategy.
   - Useful to validate conversion from native GPU resources to KAI GPU scheduling.

4) `gpu-rebalancing-policy.yaml`
   - Namespaced `GpuRebalancingPolicy` that emits GPU upsize recommendations from Prometheus.
   - Demonstrates threshold/upsize-metrics-window/max-upsize fields and Prometheus label mapping.

5) `gpu-consolidation-policy.yaml`
   - Cluster-scoped `GpuConsolidationPolicy` examples for two separate compatibility pools.
   - Demonstrates required `spec.nodeSelector` with both `matchLabels` and `matchExpressions`.
   - Shows the intended "one policy per compatible pool" setup.
   - Consolidation drains the selected node by evicting all evictable pods on it, including pods without owners such as static pods.

Notes:
- GPU is configured as `gpu` in policy requests.
- GPU must not be configured under limits.
- Depending on scheduler mode, runtime GPU state may be represented by `nvidia.com/gpu` resources or `gpu-fraction` annotation.
- Use numeric GPU values like `0.25`, `0.5`, `1`, `4` in examples.

Apply examples:

```sh
kubectl apply -f examples/gpus/simple-static-gpu-vanilla.yaml
kubectl apply -f examples/gpus/simple-static-gpu-kai.yaml
kubectl apply -f examples/gpus/simple-static-gpu-vanilla-2kai.yaml
kubectl apply -f examples/gpus/gpu-rebalancing-policy.yaml
kubectl apply -f examples/gpus/gpu-consolidation-policy.yaml
```
