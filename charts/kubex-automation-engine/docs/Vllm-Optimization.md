# vLLM optimization policy

`VllmOptimizationPolicy` can tune `--max-num-batched-tokens`, `--max-num-seqs`, and opt-in `--quantization` backend transitions for one Deployment or KubeAI Model. It compares complete policy-controlled configurations and replaces pods when applying a trial. It does not change GPU allocation, replica count, KV-cache size, or other vLLM arguments. Pod disruption during trials is expected.

## E2e fixture prerequisites

The live optimization tests require a real NVIDIA GPU cluster and the e2e Prometheus scrape fixture. Observed and batch-only cases use pinned vLLM image `vllm/vllm-openai@sha256:014a95f21c9edf6abe0aea6b07353f96baa4ec291c427bb1176dc7c93a85845c`. Native-capacity cases use the nightly artifact pinned to `vllm/vllm-openai@sha256:8896fd7b29fc4ee7ce839f146cb4757984c476ec79d0234ffa8c995dee6e339a` to exercise newer native metadata:

- Full attention uses `Qwen/Qwen2.5-0.5B-Instruct` at revision `7ae557604adf67be50417f59c2c2f167def9a775`, with the hybrid KV-cache manager disabled.
- Hybrid uses the sliding-window/full-attention `google/gemma-2-2b-it` at revision `299a8560bedf22ed1c72a8a11e7dce4a7f9f51f8`, confirmed through the Hugging Face model API, with the hybrid manager enabled. Accept the model's gated license on Hugging Face and export `HF_TOKEN` for an account with approved access. Without `HF_TOKEN`, hybrid live cases skip. The test creates a Secret in its per-test namespace and passes `HF_TOKEN` through `secretKeyRef` only for hybrid pods. Tokens never appear in workload manifests; namespace cleanup removes the Secret.

`NativeCapacity` live cases require `vllm:cache_config_info` with namespace, pod, engine, and finite positive `kv_cache_max_concurrency` labels, and gauge value `1`. The pinned image's metadata capability has not been verified on a live GPU here. If that image does not expose this metadata, the native fixture is blocked and the test fails with a capability error. Do not substitute observed sizing, infer concurrency from block counts, or add model/image identity rules to make it pass. Observed-cache and batch-only cases do not require this native metadata.

Marlin GPU cases use the same pinned stable vLLM image with `Qwen/Qwen2.5-0.5B-Instruct-AWQ` at revision `db09cd27ead7fee40cdee309693cf83601b9c899`, or `Qwen/Qwen2.5-0.5B-Instruct-GPTQ-Int4` at revision `c34a4a91629f09f73a285f32dbd26106b033c654`. Both revisions and quantization configurations were confirmed through the Hugging Face API. They require Marlin-compatible NVIDIA hardware. The KubeAI timeout case requires the installed `nvidia-gpu-l4:1` resource profile and deliberately uses a `1s` startup timeout. Run only the targeted cases against an existing GPU context:

```sh
python -m pytest test/e2e/tests/test_vllm_optimization.py -k marlin --gpu-suite --skip-kind-bootstrap --kube-context <existing-context>
```

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
      sizingBasis: ObservedCacheUsage
      targetCacheUtilization: 0.8
```

The target, latency limits, evaluation settings, and at least one formula or experiment are required. The target, including its container, is immutable. `formulaOrder` selects the `maxNumSeqs` formula; `experimentOrder` selects `maxNumBatchedTokens`, `awqMarlin`, or `gptqMarlin` experiments. Either list may be empty, but not both. The example ranges are illustrative. Keep bounds valid for the model and vLLM configuration. Without chunked prefill, the batch-token limit must accommodate the model's maximum sequence length.

## Defaults and metrics

Defaults come from the CRD schema:

| Setting | Default |
| --- | ---: |
| Objective weights, throughput / p50 TTFT / GPU compute / GPU memory | `25 / 25 / 25 / 25` |
| Objective weights, p95 TTFT / p95 inter-token latency | `0 / 0` |
| GPU compute / GPU memory direction | `Maximize / Minimize` |
| `minScoreGain` | `2` |
| `maxTrialsPerSearch` | `12` |
| `parameters.awqMarlin.startupTimeout` / `parameters.gptqMarlin.startupTimeout` | `10m` |
| `monitorInterval` / `stableFor` / `rescanInterval` | `5m / 15m / 6h` |
| `trafficChangeThresholdPercent` | `20` |
| `tolerancePercent` / `minBackloggedFraction` | `10 / 0.8` |
| `formulaOrder` / `experimentOrder` | required, no defaults |

Omitted `maxRegressionPercent` means no relative regression cap; an explicit `0` forbids regression. Weights are normalized during scoring, so they do not need to total 100. The optional `spec.prometheus` block defaults to these vLLM metric families:

- Counters: `vllm:generation_tokens_total`, `vllm:request_success_total`.
- Histograms: `vllm:time_to_first_token_seconds`, `vllm:inter_token_latency_seconds`, `vllm:request_prompt_tokens`, `vllm:request_generation_tokens`.
- Gauges: `vllm:num_requests_running`, `vllm:num_requests_waiting`.
- Observed formula gauge: `spec.prometheus.kvCacheUsageGauge`, default `vllm:kv_cache_usage_perc`. Despite its name, this gauge is a fraction, not a value from 0 to 100.
- Native formula metadata: `vllm:cache_config_info`, with the `kv_cache_max_concurrency` label.

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

Traffic comparison requires valid measurements and mean prompt/output lengths within `tolerancePercent` against both the fresh baseline and fixed search reference. When both windows meet `minBackloggedFraction`, completed-request rates may differ. Otherwise, demand-limited windows normally require comparable completion rates and matching backlog eligibility. A demand-limited candidate is also comparable if its completed-request rate strictly increases and offered concurrency, `meanRunningRequests + meanWaitingRequests`, stays within ±`tolerancePercent` of each comparison window. Rates use each window's exact duration. This exception allows queue drainage even when backlog eligibility changes, such as concurrency of 3.8 versus 3.93 at the default 10% tolerance.

Throughput remains omitted from a demand-limited candidate's score, with the original weight denominator unchanged. Latency limits and fixed-reference regression budgets still apply. Concurrency is a demand proxy, not proof of saturation or identical traffic. Run controlled tests before relying on the comparison.

For the `maxNumBatchedTokens` experiment, a parseable starting value outside the range is measured without clamping it. The first candidate enters the range at its nearest boundary, which may exceed `step`. Subsequent candidates use stepped in-range neighbors. Acceptance still requires measured evidence, and rollback preserves the exact prior value, even outside the range. Exhausting neighboring candidates is a local-search limit, not proof of a global optimum.

## Formula and experiment phases

`formulaOrder` and `experimentOrder` are required lists. At least one must be nonempty. `formulaOrder` currently accepts `maxNumSeqs`; `experimentOrder` accepts `maxNumBatchedTokens`, `awqMarlin`, and `gptqMarlin`. Combined policies run Initial formulas → Experiments → Final formulas → Monitoring. The final pass runs once in `formulaOrder` using fresh measurements of the retained post-experiment configuration, even if no experiment was accepted. Formula-only policies run a single formula pass; experiment-only policies run no formulas. Use an empty list for a phase you do not need.

The `maxNumSeqs` formula has two explicit modes:

- `ObservedCacheUsage` computes `floor(targetCacheUtilization × meanRunningRequests / meanCacheUsage)` independently for every engine. `targetCacheUtilization` is required and must be strictly between 0 and 1. Running-request and cache-usage means come from the same measurement window and engine. This estimates capacity for the observed workload, including request lengths and prefix reuse; it is not a guarantee for a different traffic mix.
- `NativeCapacity` computes `floor(kv_cache_max_concurrency)` for each engine from native `vllm:cache_config_info` metadata. This is vLLM's reported maximum-context concurrency, not observed average-request capacity. `targetCacheUtilization` is forbidden in this mode.

The raw estimate is the minimum across every engine in every ready pod. An estimate below `min` skips the formula; the controller never raises it to imply that the minimum fits. Otherwise it caps the estimate at `max` and an explicit `maxNumBatchedTokens` value when present. It never guesses absent vLLM arguments. `maxNumSeqs` requires `min`, `max`, and `sizingBasis`; it has no `step` or default sizing basis.

Formula eligibility depends on scoped, valid telemetry, not image digest, model identity, revision, or a single-engine/full-attention allowlist. Multiple engines and hybrid cache layouts use the same per-engine calculation. Missing engine labels, mismatched engine coverage, inconsistent scrape copies, or unusable inputs skip the formula rather than reconstructing cache capacity from block counts. The batch-token experiment can still run. Zero cache usage cannot support observed sizing. Prefix sharing and requests progressing through prefill and decode change the relationship between occupancy and running requests. Changed traffic can invalidate that extrapolation. Engine population coverage uses Prometheus presence at one-second resolution over the recorded baseline; it cannot detect changes between scrapes. Formula evidence records the measurement window, per-engine inputs and capacities, raw minimum, bounds, and final target.

After formula improvements, `experimentOrder` runs bounded neighbor trials for `maxNumBatchedTokens` using its `min`, `max`, and `step`. These phases share the same objective, latency constraints, traffic-comparison rules, and fixed search reference. A formula is a sizing proposal, not a performance guarantee. Both formula and experiment candidates are evaluated statistically and can roll back. The policy does not need to find a winning candidate to complete a search.

Each persisted formula or experiment candidate plan consumes an attempt, whether accepted, rejected, failed, or deferred. Skipped unchanged or invalid proposals consume none. `maxTrialsPerSearch` defaults to 12 and must be at least one; it limits the initial formulas and experiments together. Combined policies then allow at most one additional candidate per formula in the final pass, beyond that limit. Each final formula gets one opportunity: acceptance, rejection, failure, or deferral consumes it without retry. A final pass does not restart experiments. Formula results and cursors persist across controller restarts. Experiments begin with a fresh baseline of the retained configuration and never change sequence count. Reconciliation retries of the same plan do not count again. Deferred experimental candidates can be retried as new attempts. Active evaluation and required rollback finish before the next phase or monitoring begins. Unmet latency constraints still report failure. Traffic changes or periodic rescans can start a new search.

Experiments remember measured baselines and tried candidates across accepted improvements within a search. History tracks complete configurations, so a different sequence count or backend makes a configuration eligible again. New searches start with fresh history and can revisit configurations. Deferred experimental candidates remain eligible for retry.

The batch-token experiment is bounded local search, not an exhaustive grid or a global optimum. It does not jump beyond rejected neighbors to reach more distant values such as 128. Twelve 30-minute baselines and 15-minute trials require about nine hours of measurement alone, excluding rollouts, cooldown, and additional waits. The final formula pass adds measurement time beyond that budget and can cause additional rollouts and rollbacks. Each candidate and rollback can disrupt serving pods.

## Opt-in Marlin backend experiments

Marlin experiments change only an explicit backend argument, never the checkpoint, revision, weights, or quantization format:

| Experiment | Required current argument | Candidate |
| --- | --- | --- |
| `awqMarlin` | `--quantization=awq` | `--quantization=awq_marlin` |
| `gptqMarlin` | `--quantization=gptq` | `--quantization=gptq_marlin` |

Absent, unrelated, or already-Marlin backend values do not produce a candidate. The controller does not infer a backend from checkpoint metadata. Compatible models without an explicit backend may already use Marlin automatically. Both split and equals argument syntax are supported. To enable AWQ alone, use:

```yaml
formulaOrder: []
experimentOrder: [awqMarlin]
parameters:
  awqMarlin: {} # startupTimeout defaults to 10m
```

Use `gptqMarlin` and `parameters.gptqMarlin` for GPTQ. You may order either alongside batch-token experiments. Each has one explicit transition, not a numeric search range. A compatible quantized checkpoint and working baseline are prerequisites. GPU architecture, vLLM build, and checkpoint layout determine whether the backend starts. Compatibility is established by a controlled trial on the target, not an identity allowlist or a promise that all AWQ/GPTQ checkpoints work. Benchmarking does not validate model output quality.

`startupTimeout` must be positive and applies only to initial candidate application while replacement pods become ready, including KubeAI Models without Deployment progress deadlines. Its deadline starts at persisted `status.plan.createdAt`; reconciliation retries and controller restarts do not reset it. The controller must observe initial readiness before that deadline, so controller downtime counts toward the timeout. It is not an evaluation-window, rollback, baseline-startup, or cleanup timeout. A candidate startup timeout fails the trial and starts rollback to the prior complete configuration. Once ready, the candidate uses the usual measurement, traffic, scoring, and rejection rules. An incompatible startup is not a reason to change the checkpoint automatically.

Both Marlin experiments share ownership of `--quantization`. Removing one while the other remains enabled retains that argument's selected and original snapshots and child-policy ownership. Only removing the last Marlin experiment restores the latest original declaration and releases ownership after convergence. Deleting the policy also restores it. Do not run another argument policy that owns `--quantization` on the same target.

## Workload changes

The controller captures controlled arguments from Deployment template args or KubeAI Model `spec.args` in `status.declaredConfiguration`. Tuner-mutated pod arguments never count as declaration edits. Equivalent split and equals syntax have the same declaration. An absent argument remains absent; the controller does not guess vLLM defaults.

Changing a declaration replaces that parameter's original restoration value and restarts it from the new declaration. Unchanged parameters retain their original and selected values. Scaling or editing another workload field cancels active trials, waits for the retained configuration and rollout to converge, then discards old measurements, tried configurations, and monitoring triggers before collecting a fresh baseline. Recreating the target workload still requires a new policy.

Adding an improvement captures its current declaration and original restoration value before tuning. Removing an improvement from `formulaOrder` or `experimentOrder`, or removing its parameter settings, restores its latest original value, waits for rollout convergence, then releases that argument from snapshots and the owned child policy. Both edits discard stale measurements and start a fresh baseline. Unchanged arguments retain their selected values.

Changing either order is a policy-generation change. An active trial first rolls back to its prior configuration. The controller then discards stale measurements, tried configurations, and monitoring triggers before searching with the new phase order. An edit during monitoring starts that fresh search directly.

Runtime argument drift follows the same declaration and convergence checks. The controller reapplies retained selections only when policy ownership and conflict checks permit it. Unsupported or ambiguous argument locations, unavailable metrics, insufficient observations, and unready pods still block measurement. A usable baseline is required; the controller does not diagnose startup failures or try unmeasured startup repairs.

## Monitoring and status

After a search settles, the controller measures the selected configuration again and uses that as the monitoring reference. It starts a new search after a sustained traffic change, sustained latency-limit violation, periodic rescan deadline, or policy change. Invalid observations and missed monitoring checks reset sustained-change detection. Candidate changes respect cooldown; a required rollback does not.

`status.phase` is policy-wide. `Monitoring` means the policy is settled and will keep checking, not that it has stopped adapting. Root `Ready=True` and `status.outcome=Success` require a fresh compliant measurement in monitoring. Pending observations do not report success. Fresh unmet latency constraints report failure, including when local search is exhausted. `status.selectedConfiguration` and `status.originalConfiguration` are authoritative complete argument snapshots. `status.parameters` contains derived summaries for numeric controls only; quantization appears in the complete configuration snapshots, and `status.lastDecision` retains the latest candidate outcome. `status.search.attempts` counts attempts; `status.search.trialResults` retains current-search decisions with complete candidate configurations, raw throughput, p50/p95 latency and optional GPU measurements, scores, and scored metrics. These records reset with the next search.

`status.search.completion` preserves its own retained configuration and measurement, completion reason, scored metrics, and score calculated directly against the fixed search reference. It does not sum gains from different windows. `comparisonReason` explains unavailable comparisons, such as incomparable traffic or no scoring opportunity. `recoveryProgress` reports reduced hard-limit violations separately from objective improvement. Later monitoring measurements do not change completion evidence.

Lifecycle events appear on the `VllmOptimizationPolicy`, not the target workload. Normal events describe search triggers, baseline collection, candidate preparation and evaluation, acceptance, rejection or deferral, rollback, monitoring entry, and deletion cleanup. Messages include controlled argument values and score gain when available. Restoration of an originally absent argument is reported as removal, not as a guessed vLLM default. Warning events report holds, unavailable measurements, policy conflicts, and rollout failures. Unchanged waits and holds do not repeat, and routine monitoring polls emit no events. `CleanupComplete` follows successful cleanup finalizer removal.

```sh
kubectl describe vllmoptimizationpolicy <name>
```

## Controller settings metrics

The existing controller `/metrics` endpoint exposes gauges `automation_controller_vllm_optimization_max_num_batched_tokens` and `automation_controller_vllm_optimization_max_num_seqs` through the metrics Service. Labels are `policy`, `target_namespace`, `target_kind`, `target_name`, `container`, and `configuration`. The container label uses the resolved `status.containerName`.

`configuration="selected"` is the retained setting; `configuration="candidate"` is the active trial setting. Values come from cached `status.parameters.maxNumBatchedTokens` and `status.parameters.maxNumSeqs`. Unknown or absent argument values produce no series, and the controller never invents vLLM defaults. Candidate series disappear after acceptance or during rollback. All series disappear when a policy is deleting or deleted. Cache-read failures surface as collection errors.

`configuration="current"` is the controller's current target. It uses the candidate value while a plan is `Applying` or `Evaluating`, and the selected value during `RollingBack` or when no plan exists. A stale candidate summary without a plan does not affect `current`. If the corresponding value is unknown or absent, no `current` series is emitted.

For dashboards showing the current target, deduplicate controller replicas with:

```promql
max by (
  policy, target_namespace, target_kind,
  target_name, container, configuration
) (
  automation_controller_vllm_optimization_max_num_batched_tokens{configuration="current"}
)
```

These metrics describe controller settings, not settings confirmed on every running pod. In particular, `current` can change before every pod has completed rollout. Cache propagation can briefly differ between controller replicas.

## Rollback, deletion, and upgrade

A rejected trial restores the prior complete configuration. Deleting the policy restores each latest original explicit argument, or removes it if that declaration was absent. User declaration edits override earlier tuning and restoration values. Wait for replacement pods and finalizer removal before considering cleanup complete. Do not remove the finalizer manually.

This incompatible API replaces the old `ObservedMean` and `MaxModelLen` sizing bases with `ObservedCacheUsage` and `NativeCapacity`. Older policies using `parameterOrder` must also move to required `formulaOrder` and `experimentOrder` lists. Existing policies must be recreated. Save their specs, then delete the policies while the old controller is still running. Wait for original arguments to be restored, owned `ContainerArgsPolicy` objects to be deleted, and policy finalizers to be removed. Only then upgrade the CRD and controller. Replace `parameterOrder` with `formulaOrder: [maxNumSeqs]` and `experimentOrder: [maxNumBatchedTokens]`, using an empty list for disabled improvements. Remove `parameters.maxNumSeqs.step`. Choose `ObservedCacheUsage` with an explicit `targetCacheUtilization`, or `NativeCapacity` without that field. There is no automatic translation of old block-based sizing. Recreate the policies with the translated specs. Do not clear status or finalizers by hand.

The tuner measures performance at a fixed allocation. It does not prove that fewer GPUs or replicas are safe, and it does not claim a cost reduction.
