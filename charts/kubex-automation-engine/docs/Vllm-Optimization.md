# vLLM optimization policy

`VllmOptimizationPolicy` experimentally tunes `--max-num-batched-tokens` and `--max-num-seqs` for one Deployment or KubeAI Model. It compares complete policy-controlled configurations and replaces pods when applying a trial. It does not change GPU allocation, replica count, KV-cache size, or other vLLM arguments. Pod disruption during trials is expected.

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
      weight: 70
      maxRegressionPercent: 3
    p95TTFT:
      weight: 20
      maxRegressionPercent: 10
    p95InterTokenLatency:
      weight: 10
      maxRegressionPercent: 5
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
  parameters:
    maxNumBatchedTokens:
      min: 512
      max: 2048
      step: 512
    maxNumSeqs:
      min: 8
      max: 12
      step: 4
```

The target, latency limits, evaluation settings, and at least one parameter range are required. The target, including its container, is immutable. Either `maxNumBatchedTokens` or `maxNumSeqs`, or both, can be enabled. The example ranges are illustrative, not universal recommendations. Keep bounds within values valid for the model and vLLM configuration. Without chunked prefill, the batch-token limit must accommodate the model's maximum sequence length.

## Defaults and metrics

Defaults come from the CRD schema:

| Setting | Default |
| --- | ---: |
| Objective weights, throughput / p95 TTFT / p95 inter-token latency | `100 / 0 / 0` |
| `minScoreGain` | `2` |
| `maxTrialsPerSearch` | `12` |
| `monitorInterval` / `stableFor` / `rescanInterval` | `5m / 15m / 6h` |
| `trafficChangeThresholdPercent` | `20` |
| `tolerancePercent` / `minBackloggedFraction` | `10 / 0.8` |

Omitted `maxRegressionPercent` means no relative regression cap; an explicit `0` forbids regression. Weights are normalized during scoring, so they do not need to total 100. The optional `spec.prometheus` block defaults to these vLLM metric families:

- Counters: `vllm:generation_tokens_total`, `vllm:request_success_total`.
- Histograms: `vllm:time_to_first_token_seconds`, `vllm:inter_token_latency_seconds`, `vllm:request_prompt_tokens`, `vllm:request_generation_tokens`.
- Gauges: `vllm:num_requests_running`, `vllm:num_requests_waiting`.

Histogram settings name classic Prometheus families; queries use `_bucket`, `_sum`, and `_count`. Override metric family and label names under `spec.prometheus`. The default scope labels are `namespace` and `pod`. Every returned series must include both labels. Configure the Prometheus URL and request timeout in `GlobalConfiguration`, as described in the [Global Configuration guide](./Global-Configuration.md). Missing metrics or scope labels make the policy wait rather than query unscoped data.

## How trials are scored

The controller compares each candidate with a fresh measurement of the selected configuration. Throughput contributes `ln(candidate / reference)`; latency contributes `ln(reference / candidate)`. It multiplies each value by its objective weight, normalizes by the total weight, then scales the score by 100. A candidate must meet `minScoreGain`, pass absolute latency limits, fit the regression budgets, and use comparable traffic. If the selected configuration violates a latency limit, a candidate can be accepted without `minScoreGain` when it strictly reduces at least one violation, does not worsen another, and does not introduce a violation of a previously satisfied limit. Traffic comparability, valid measurements, and fixed-reference regression budgets still apply. The controller can accept several partial improvements before reaching compliance; `Ready` stays false during recovery.

Regression budgets use the fixed measurement from the start of the search. This stops several small regressions from accumulating past the configured budget. A new search captures a new reference.

Traffic comparison uses mean prompt/output lengths, completed-request rate, running/waiting request gauges, and the fraction of the window with waiting requests. When the waiting fraction meets `minBackloggedFraction`, completed-request rate need not stay equal because a higher completion rate can be the improvement. Below that threshold, the controller treats traffic as demand-limited, requires comparable request rates, and omits throughput from the score while keeping the original weight denominator. A material change in prompt/output mix or backlog eligibility defers candidate scoring. These metrics are demand proxies, not proof of saturation or identical requests. Run controlled tests before relying on the comparison.

A parseable starting value outside the range is measured without clamping it. The first candidate enters the range at its nearest boundary, which may exceed `step`. Subsequent candidates use stepped in-range neighbors. Acceptance still requires measured evidence, and rollback preserves the exact prior value, even outside the range. Exhausting neighboring candidates is a local-search limit, not proof of a global optimum.

## Bounded joint search

The controller first tests individual one-step neighbors, batch tokens before sequences and upper values before lower values. If none yields an accepted move, it tests joint neighbors that change both arguments. After acceptance, it searches around the retained complete configuration. Rejections apply to combinations, so a rejected batch value at eight sequences can be retried at twelve sequences. It skips combinations with explicit batch tokens below explicit sequences, without guessing omitted vLLM defaults. Model-specific restrictions remain your responsibility.

Each persisted candidate plan consumes one attempt, whether accepted, rejected, failed, or deferred. Reconciliation retries of the same plan do not count again. Deferred candidates can be retried as new attempts. `maxTrialsPerSearch` defaults to 12, including for existing batch-only policies, and must be at least one. After exhaustion, active evaluation and required rollback finish before monitoring begins with `TrialBudgetExhausted`. Unmet latency constraints still report failure. A handled recovery breach does not immediately restart searches and reset the budget, though traffic changes or periodic rescans can start a new search.

This is bounded local search, not an exhaustive grid or a global optimum. Twelve complete 30-minute baselines and 15-minute trials require about nine hours of measurement alone, excluding rollouts, cooldown, and additional waits. Each candidate and rollback can disrupt serving pods.

## Workload changes

The controller captures controlled arguments from Deployment template args or KubeAI Model `spec.args` in `status.declaredConfiguration`. Tuner-mutated pod arguments never count as declaration edits. Equivalent split and equals syntax have the same declaration. An absent argument remains absent; the controller does not guess vLLM defaults.

Changing a declaration replaces that parameter's original restoration value and restarts it from the new declaration. Unchanged parameters retain their original and selected values. Scaling or editing another workload field cancels active trials, waits for the retained configuration and rollout to converge, then discards old measurements, tried configurations, and monitoring triggers before collecting a fresh baseline. Existing policies without the declaration snapshot take a one-time conservative rebaseline without policy recreation. Recreating the target workload still requires a new policy.

Adding a parameter captures its declaration and original restoration value before tuning. Removing a parameter restores its latest original value, waits for rollout convergence, then releases that argument from the owned child policy. Both edits discard stale measurements and start a fresh baseline. Unchanged parameters retain their selected values.

Runtime argument drift follows the same declaration and convergence checks. The controller reapplies retained selections only when policy ownership and conflict checks permit it. Unsupported or ambiguous argument locations, unavailable metrics, insufficient observations, and unready pods still block measurement. A usable baseline is required; the controller does not diagnose startup failures or try unmeasured startup repairs.

## Monitoring and status

After a search settles, the controller measures the selected configuration again and uses that as the monitoring reference. It starts a new search after a sustained traffic change, sustained latency-limit violation, periodic rescan deadline, or policy change. Invalid observations and missed monitoring checks reset sustained-change detection. Candidate changes respect cooldown; a required rollback does not.

`status.phase` is policy-wide. `Monitoring` means the policy is settled and will keep checking, not that it has stopped adapting. Root `Ready=True` and `status.outcome=Success` require a fresh compliant measurement in monitoring. Pending observations do not report success. Fresh unmet latency constraints report failure, including when local search is exhausted. `status.selectedConfiguration` and `status.originalConfiguration` are authoritative complete argument snapshots. `status.parameters` contains derived summaries for both arguments, and `status.lastDecision` retains the latest candidate outcome. `status.search.attempts` counts attempts; `status.search.trialResults` retains current-search decisions with complete candidate configurations, raw throughput and latency measurements, scores, and scored metrics. These records reset with the next search.

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

The declaration snapshot is additive; policies using the current configuration-based API do not need recreation. For upgrades from the older experimental API before complete configuration status and adaptation settings, save policy specs, delete the policies with the old controller running, and wait for argument restoration, owned `ContainerArgsPolicy` deletion, and finalizer removal. Upgrade the controller and CRD only after cleanup finishes, then translate and recreate the saved policies. Do not clear status or finalizers by hand.

The tuner measures performance at a fixed allocation. It does not prove that fewer GPUs or replicas are safe, and it does not claim a cost reduction.
