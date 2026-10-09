# vLLM optimization policy

`VllmOptimizationPolicy` can tune `--max-num-batched-tokens`, `--max-num-seqs`, or both for one Deployment or KubeAI Model. It compares complete policy-controlled configurations and replaces pods when applying a trial. It does not change GPU allocation, replica count, KV-cache size, or other vLLM arguments. Pod disruption during trials is expected.

## Configure a policy

```yaml
apiVersion: rightsizing.kubex.ai/v1alpha1
kind: VllmOptimizationPolicy
metadata:
  name: serving-model
spec:
  target:
    apiVersion: apps/v1
    kind: Deployment
    namespace: serving
    name: my-model
    container: vllm
  objective:
    throughput:
      weight: 25
    p50TTFT:
      weight: 25
    gpuComputeUtilization:
      weight: 25
      direction: Maximize
    gpuMemoryFootprint:
      weight: 25
      direction: Minimize
    p95TTFT:
      weight: 0
    p95InterTokenLatency:
      weight: 0
    minScoreGain: 2
  constraints:
    maxP95TTFT: 2s
    maxP95InterTokenLatency: 100ms
  evaluation:
    baselineWindow: 30m
    trialWindow: 15m
    cooldown: 30m
    minFinishedRequests: 100
    maxTrialsPerSearch: 12
  adaptation:
    monitorInterval: 5m
    stableFor: 15m
    rescanInterval: 6h
    trafficChangeThresholdPercent: 20
  trafficComparison:
    tolerancePercent: 10
    minBackloggedFraction: 0.8
  formulaOrder:
    - maxNumSeqs
  experimentOrder:
    - maxNumBatchedTokens
  parameters:
    maxNumBatchedTokens:
      min: 512
      max: 2048
      step: 512
    maxNumSeqs:
      min: 8
      max: 12
      sizingBasis: ObservedMean
```

The target, latency limits, evaluation settings, and at least one formula or experiment are required. The target, including its container, is immutable. `formulaOrder` selects the `maxNumSeqs` formula; `experimentOrder` selects the `maxNumBatchedTokens` experiment. Either list may be empty, but not both. The example ranges are illustrative. Keep bounds valid for the model and vLLM configuration. Without chunked prefill, the batch-token limit must accommodate the model's maximum sequence length.

## Defaults and metrics

Defaults come from the CRD schema:

| Setting | Default |
| --- | ---: |
| Objective weights, throughput / p50 TTFT / GPU compute / GPU memory | `25 / 25 / 25 / 25` |
| Objective weights, p95 TTFT / p95 inter-token latency | `0 / 0` |
| GPU compute / GPU memory direction | `Maximize / Minimize` |
| `minScoreGain` | `2` |
| `maxTrialsPerSearch` | `12` |
| `monitorInterval` / `stableFor` / `rescanInterval` | `5m / 15m / 6h` |
| `trafficChangeThresholdPercent` | `20` |
| `tolerancePercent` / `minBackloggedFraction` | `10 / 0.8` |
| `formulaOrder` / `experimentOrder` | required, no defaults |

Omitted `maxRegressionPercent` means no relative regression cap; an explicit `0` forbids regression. Weights are normalized during scoring, so they do not need to total 100. The optional `spec.prometheus` block defaults to these vLLM metric families:

- Counters: `vllm:generation_tokens_total`, `vllm:request_success_total`.
- Histograms: `vllm:time_to_first_token_seconds`, `vllm:inter_token_latency_seconds`, `vllm:request_prompt_tokens`, `vllm:request_generation_tokens`.
- Gauges: `vllm:num_requests_running`, `vllm:num_requests_waiting`.

The default policy also requires [GPU Process Exporter](https://github.com/densify-dev/gpu-process-exporter) metrics:

| Setting under `spec.prometheus` | Default | Collection |
| --- | --- | --- |
| `gpuComputeUtilizationCounter` | `kubex_gpu_container_sm_utilization_percent_seconds_total` | `rate` over the measurement window |
| `gpuMemoryFootprintGauge` | `kubex_gpu_container_memory_footprint_percent` | `avg_over_time` over the measurement window |
| `gpuContainerLabel` | `container` | Resolved serving container |
| `gpuDeviceLabel` | `gpu_uuid` | Individual GPU device |

GPU queries select the target namespace, ready pods, and serving container. The controller applies `rate` before aggregation, deduplicates scrape copies with `max` per pod/device, and takes an equal-device mean across the target. Every ready pod must have valid required GPU data. Missing data defers measurement; zero is valid, and values above 100 are not clamped. Memory footprint uses the exporter's denominator. For fractional KAI allocations, that is allocated memory, not whole-device capacity.

Omitted GPU objective blocks receive the defaults above. Explicit GPU blocks must declare `weight` and `direction`, which can be `Maximize` or `Minimize` for either metric. To run without GPU metrics, disable both objectives and omit their regression caps:

```yaml
objective:
  gpuComputeUtilization:
    weight: 0
    direction: Maximize
  gpuMemoryFootprint:
    weight: 0
    direction: Minimize
```

Weight zero disables scoring. A configured `maxRegressionPercent` still requires measurement, even with zero weight. GPU metrics are queried only when weighted or capped. At least one objective must have positive weight. Both p95 absolute latency constraints remain mandatory regardless of weights.

Histogram settings name classic Prometheus families; queries use `_bucket`, `_sum`, and `_count`. Override metric family and label names under `spec.prometheus`. The default scope labels are `namespace` and `pod`. Every returned series must include both labels. Configure the Prometheus URL and request timeout in `GlobalConfiguration`, as described in the [Global Configuration guide](./Global-Configuration.md). Missing metrics or scope labels make the policy wait rather than query unscoped data.

## How trials are scored

The controller compares each candidate with a fresh measurement of the selected configuration. Throughput contributes `100 × ln(candidate / baseline)`. Latencies, including p50 TTFT, contribute `100 × ln(baseline / candidate)`. GPU objectives contribute percentage-point changes, `candidate - baseline` for `Maximize` and `baseline - candidate` for `Minimize`. This supports zero GPU utilization without logarithms. The final score is the weighted mean of contributions using the full configured weight denominator. TTFT quantiles come from the aggregate histogram, not an average of pod quantiles. A candidate must meet `minScoreGain`, pass absolute latency limits, fit the regression budgets, and use comparable traffic. If the selected configuration violates a latency limit, a candidate can be accepted without `minScoreGain` when it strictly reduces at least one violation, does not worsen another, and does not introduce a violation of a previously satisfied limit. Traffic comparability, valid measurements, and fixed-reference regression budgets still apply. The controller can accept several partial improvements before reaching compliance; `Ready` stays false during recovery.

Regression budgets use the fixed measurement from the start of the search. An unfavorable change must not exceed `reference × maxRegressionPercent / 100`, respecting each objective's direction. A zero reference permits no unfavorable change. This stops several small regressions from accumulating past the configured budget. A new search captures a new reference.

Traffic comparison uses mean prompt/output lengths, completed-request rate, running/waiting request gauges, and the fraction of the window with waiting requests. When the waiting fraction meets `minBackloggedFraction`, completed-request rate need not stay equal because a higher completion rate can be the improvement. Below that threshold, the controller treats traffic as demand-limited, requires comparable request rates, and omits throughput from the score while keeping the original weight denominator. A material change in prompt/output mix or backlog eligibility defers candidate scoring. These metrics are demand proxies, not proof of saturation or identical requests. Run controlled tests before relying on the comparison.

For the `maxNumBatchedTokens` experiment, a parseable starting value outside the range is measured without clamping it. The first candidate enters the range at its nearest boundary, which may exceed `step`. Subsequent candidates use stepped in-range neighbors. Acceptance still requires measured evidence, and rollback preserves the exact prior value, even outside the range. Exhausting neighboring candidates is a local-search limit, not proof of a global optimum.

## Formula and experiment phases

`formulaOrder` and `experimentOrder` are required lists. At least one must be nonempty. `formulaOrder` currently accepts `maxNumSeqs`; `experimentOrder` accepts `maxNumBatchedTokens`. The controller runs formula improvements before experiments. Use an empty list for a phase you do not need.

The `maxNumSeqs` formula supports `ObservedMean` and `MaxModelLen`. `ObservedMean` uses the measured mean prompt plus output length for each ready pod. `MaxModelLen` uses an explicit `--max-model-len` argument. Formula sizing requires the verified single-engine, uniform full-attention workload. Native cache metadata does not identify every hybrid layout, so this first implementation accepts only `Qwen/Qwen2.5-0.5B-Instruct` at revision `7ae557604adf67be50417f59c2c2f167def9a775`, served by `vllm/vllm-openai@sha256:014a95f21c9edf6abe0aea6b07353f96baa4ec291c427bb1176dc7c93a85845c`. Other images or models, layout overrides, speculative decoding, and multi-engine configurations skip the formula. Batch-only optimization is not subject to this restriction.

The formula reads native `vllm:cache_config_info` metadata, including `engine`, `num_gpu_blocks`, and `block_size`. Each ready pod must report exactly one engine with consistent positive block metadata. Missing or invalid metadata, multiple engines, missing observed prompt/output means, or a missing explicit max model length makes that formula improvement skip; the batch-token experiment can still run. The formula reserves one null cache block per engine, computes blocks per sequence as `ceil(sequenceLength / blockSize)`, then estimates capacity as `(num_gpu_blocks - 1) // blocksPerSequence`. It takes the minimum capacity across ready pods, clamps that result to the configured `min` and `max`, and also caps it at an explicitly configured `maxNumBatchedTokens`. It does not guess vLLM defaults. `maxNumSeqs` requires `min`, `max`, and `sizingBasis`; it has no `step`.

After formula improvements, `experimentOrder` runs bounded neighbor trials for `maxNumBatchedTokens` using its `min`, `max`, and `step`. These phases share the same objective, latency constraints, traffic-comparison rules, and fixed search reference. A formula is a sizing proposal, not a performance guarantee. Both formula and experiment candidates are evaluated statistically and can roll back. The policy does not need to find a winning candidate to complete a search.

Each persisted formula or experiment candidate plan consumes an attempt, whether accepted, rejected, failed, or deferred. Skips consume none. A formula runs once per cycle, including after deferral; its result and cursor persist across controller restarts. Experiments begin with a fresh baseline of the retained configuration and never change sequence count. Reconciliation retries of the same plan do not count again. Deferred experimental candidates can be retried as new attempts. `maxTrialsPerSearch` defaults to 12 and must be at least one. After exhaustion, active evaluation and required rollback finish before monitoring begins. Unmet latency constraints still report failure. Traffic changes or periodic rescans can start a new search.

The batch-token experiment is bounded local search, not an exhaustive grid or a global optimum. Twelve 30-minute baselines and 15-minute trials require about nine hours of measurement alone, excluding rollouts, cooldown, and additional waits. Each candidate and rollback can disrupt serving pods.

## Workload changes

The controller captures controlled arguments from Deployment template args or KubeAI Model `spec.args` in `status.declaredConfiguration`. Tuner-mutated pod arguments never count as declaration edits. Equivalent split and equals syntax have the same declaration. An absent argument remains absent; the controller does not guess vLLM defaults.

Changing a declaration replaces that parameter's original restoration value and restarts it from the new declaration. Unchanged parameters retain their original and selected values. Scaling or editing another workload field cancels active trials, waits for the retained configuration and rollout to converge, then discards old measurements, tried configurations, and monitoring triggers before collecting a fresh baseline. Recreating the target workload still requires a new policy.

Adding an improvement captures its current declaration and original restoration value before tuning. Removing an improvement from `formulaOrder` or `experimentOrder`, or removing its parameter settings, restores its latest original value, waits for rollout convergence, then releases that argument from snapshots and the owned child policy. Both edits discard stale measurements and start a fresh baseline. Unchanged arguments retain their selected values.

Changing either order is a policy-generation change. An active trial first rolls back to its prior configuration. The controller then discards stale measurements, tried configurations, and monitoring triggers before searching with the new phase order. An edit during monitoring starts that fresh search directly.

Runtime argument drift follows the same declaration and convergence checks. The controller reapplies retained selections only when policy ownership and conflict checks permit it. Unsupported or ambiguous argument locations, unavailable metrics, insufficient observations, and unready pods still block measurement. A usable baseline is required; the controller does not diagnose startup failures or try unmeasured startup repairs.

## Monitoring and status

After a search settles, the controller measures the selected configuration again and uses that as the monitoring reference. It starts a new search after a sustained traffic change, sustained latency-limit violation, periodic rescan deadline, or policy change. Invalid observations and missed monitoring checks reset sustained-change detection. Candidate changes respect cooldown; a required rollback does not.

`status.phase` is policy-wide. `Monitoring` means the policy is settled and will keep checking, not that it has stopped adapting. Root `Ready=True` and `status.outcome=Success` require a fresh compliant measurement in monitoring. Pending observations do not report success. Fresh unmet latency constraints report failure, including when local search is exhausted. `status.selectedConfiguration` and `status.originalConfiguration` are authoritative complete argument snapshots. `status.parameters` contains derived summaries for both arguments, and `status.lastDecision` retains the latest candidate outcome. `status.search.attempts` counts attempts; `status.search.trialResults` retains current-search decisions with complete candidate configurations, raw throughput, p50/p95 latency and optional GPU measurements, scores, and scored metrics. These records reset with the next search.

`status.search.completion` preserves its own retained configuration and measurement, completion reason, scored metrics, and score calculated directly against the fixed search reference. It does not sum gains from different windows. `comparisonReason` explains unavailable comparisons, such as incomparable traffic or no scoring opportunity. `recoveryProgress` reports reduced hard-limit violations separately from objective improvement. Later monitoring measurements do not change completion evidence.

Lifecycle events appear on the `VllmOptimizationPolicy`, not the target workload. Normal events describe search triggers, baseline collection, candidate preparation and evaluation, acceptance, rejection or deferral, rollback, monitoring entry, and deletion cleanup. Messages include controlled argument values and score gain when available. Restoration of an originally absent argument is reported as removal, not as a guessed vLLM default. Warning events report holds, unavailable measurements, policy conflicts, and rollout failures. Unchanged waits and holds do not repeat, and routine monitoring polls emit no events. `CleanupComplete` follows successful cleanup finalizer removal.

```sh
kubectl describe vllmoptimizationpolicy <name>
```

## Controller settings metrics

The existing controller `/metrics` endpoint exposes gauges `automation_controller_vllm_optimization_max_num_batched_tokens` and `automation_controller_vllm_optimization_max_num_seqs` through the metrics Service. Labels are `policy`, `target_namespace`, `target_kind`, `target_name`, `container`, and `configuration`. The container label uses the resolved `status.containerName`.

`configuration="selected"` is the retained setting; `configuration="candidate"` is the active trial setting. Values come from cached `status.parameters.maxNumBatchedTokens` and `status.parameters.maxNumSeqs`. Unknown or absent argument values produce no series, and the controller never invents vLLM defaults. Candidate series disappear after acceptance or during rollback. All series disappear when a policy is deleting or deleted. Cache-read failures surface as collection errors.

For dashboards, deduplicate controller replicas with:

```promql
max by (
  policy, target_namespace, target_kind,
  target_name, container, configuration
) (
  automation_controller_vllm_optimization_max_num_batched_tokens
)
```

These metrics describe controller settings, not proof that every pod has completed rollout. Cache propagation can briefly differ between controller replicas.

## Rollback, deletion, and upgrade

A rejected trial restores the prior complete configuration. Deleting the policy restores each latest original explicit argument, or removes it if that declaration was absent. User declaration edits override earlier tuning and restoration values. Wait for replacement pods and finalizer removal before considering cleanup complete. Do not remove the finalizer manually.

This API replaces `parameterOrder` with required `formulaOrder` and `experimentOrder` fields, so existing policies must be recreated. Save their specs, then delete the policies while the old controller is still running. Wait for original arguments to be restored, owned `ContainerArgsPolicy` objects to be deleted, and policy finalizers to be removed. Only then upgrade the CRD and controller. Replace `parameterOrder` with `formulaOrder: [maxNumSeqs]` and `experimentOrder: [maxNumBatchedTokens]`, using an empty list for disabled improvements. Remove `parameters.maxNumSeqs.step`, choose an explicit `sizingBasis`, and recreate the policies with the translated specs. Do not clear status or finalizers by hand.

The tuner measures performance at a fixed allocation. It does not prove that fewer GPUs or replicas are safe, and it does not claim a cost reduction.
