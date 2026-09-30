# vLLM optimization policy

`VllmOptimizationPolicy` runs bounded trials of `--max-num-batched-tokens` for one `apps/v1` Deployment or KubeAI Model. It does not change GPU fractions, KV-cache sizing, or other vLLM arguments. This is experimental. No throughput gain has been measured by this implementation.

## Requirements

- Prometheus must scrape the target pods' vLLM counters and classic histogram buckets. The controller needs namespace and pod labels on every returned series. Missing series or labels put the policy in `Holding`; it will not broaden queries.
- Configure the Prometheus URL and request timeout through `GlobalConfiguration`, as described in the [Global Configuration guide](./Global-Configuration.md).
- Keep the offered load and input prompt mix fixed across windows. The controller requires both the finished-request rate and mean generated tokens per completed request to stay within 10% of baseline.
- Verify chunked prefill for the vLLM image. Without it, `maxBatchedTokens` must accommodate the model's maximum sequence length.

The metric families default to `vllm:generation_tokens_total`, `vllm:request_success_total`, `vllm:time_to_first_token_seconds`, and `vllm:inter_token_latency_seconds`. Histogram configuration names the family; queries use its `_bucket` series. Namespace and pod label names default to `namespace` and `pod`. Override these fields under `spec.prometheus` if your scrape uses different labels. Set `containerLabel` only when that label exists on every queried series.

## Example

Values below are examples, not controller defaults.

```yaml
apiVersion: rightsizing.kubex.ai/v1alpha1
kind: VllmOptimizationPolicy
metadata:
  name: serving-model
spec:
  target:
    apiVersion: kubeai.org/v1 # apps/v1 for a Deployment
    kind: Model
    namespace: serving
    name: my-model
    container: vllm
  optimizations:
    batchScheduling:
      minBatchedTokens: 2048
      maxBatchedTokens: 8192
      stepTokens: 1024
      initialObservationWindow: 30m
      evaluationWindow: 15m
      cooldown: 30m
      minFinishedRequests: 100
      maxP95TTFT: 2s
      maxP95InterTokenLatency: 100ms
```

Set `target.container` when the pod has more than one identifiable vLLM container. The target, including its container selection, cannot be changed after creation. A change to the workload spec generation puts the policy in `Holding`; create a new policy after the workload is stable.

## What the controller does

It waits for ready pods and records an untouched baseline before changing anything. KubeAI Models must expose a desired replica count and matching total and ready status counts. The baseline window restarts if the ready pod set changes. It captures `--max-num-batched-tokens` only when every ready pod agrees. An omitted argument stays omitted in status; the controller does not guess vLLM's effective default.

If the argument was absent, the first trial uses `minBatchedTokens`. Otherwise, it tests adjacent values within the configured bounds. A value is kept only if generated tokens per second improve, both configured p95 limits pass, enough requests complete, and request rate and mean generated tokens per request remain comparable. Cooldown applies between decisions. The evaluation window restarts if the replacement pod set changes. A rejected value is not retried. If changed bounds exclude the selected value, the policy holds; an active trial outside new bounds is rolled back.

The controller applies changes through an owned, narrowly scoped `ContainerArgsPolicy` with pod replacement enabled. If another `ContainerArgsPolicy` already controls the same argument, it holds instead of competing by weight. If a conflict appears mid-trial, it removes its child policy and pauses rollback until the conflict is removed or scoped away. Status conditions show the current phase and reason; a Warning event is emitted when the policy enters a new held state.

A rejected trial restores the last known-good value. When the policy is deleted, the controller restores the original explicit value, or removes the argument if it was absent. It waits for replacement pods to converge before deleting the child policy and removing its target label. A competing `ContainerArgsPolicy` pauses cleanup, leaving the finalizer in place until you remove that policy or scope it away. The Helm pre-delete hook also waits for this restoration. Do not remove the finalizer manually while a restore is in progress.

A successful trial only shows an improvement for the measured windows. Benchmark a pinned vLLM image and model at fixed load, then run a single-GPU test before treating the result as a production optimization.
