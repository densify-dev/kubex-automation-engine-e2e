# vLLM optimization policy

`VllmOptimizationPolicy` experimentally tunes `--max-num-batched-tokens` for one Deployment or KubeAI Model. It compares complete policy-controlled configurations and replaces pods when applying a trial. It does not change GPU allocation, replica count, KV-cache size, or other vLLM arguments. Pod disruption during trials is expected.

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
      min: 2048
      max: 8192
      step: 1024
```

The target, latency limits, evaluation settings, and at least one parameter range are required. The target, including its container, is immutable. `maxNumBatchedTokens` is the only supported parameter today. Keep its bounds within values valid for the model and vLLM configuration. Without chunked prefill, the batch-token limit must accommodate the model's maximum sequence length.

## Defaults and metrics

Defaults come from the CRD schema:

| Setting | Default |
| --- | ---: |
| Objective weights, throughput / p95 TTFT / p95 inter-token latency | `100 / 0 / 0` |
| `minScoreGain` | `2` |
| `monitorInterval` / `stableFor` / `rescanInterval` | `5m / 15m / 6h` |
| `trafficChangeThresholdPercent` | `20` |
| `tolerancePercent` / `minBackloggedFraction` | `10 / 0.8` |

Omitted `maxRegressionPercent` means no relative regression cap; an explicit `0` forbids regression. Weights are normalized during scoring, so they do not need to total 100. The optional `spec.prometheus` block defaults to these vLLM metric families:

- Counters: `vllm:generation_tokens_total`, `vllm:request_success_total`.
- Histograms: `vllm:time_to_first_token_seconds`, `vllm:inter_token_latency_seconds`, `vllm:request_prompt_tokens`, `vllm:request_generation_tokens`.
- Gauges: `vllm:num_requests_running`, `vllm:num_requests_waiting`.

Histogram settings name classic Prometheus families; queries use `_bucket`, `_sum`, and `_count`. Override metric family and label names under `spec.prometheus`. The default scope labels are `namespace` and `pod`. Every returned series must include both labels. Configure the Prometheus URL and request timeout in `GlobalConfiguration`, as described in the [Global Configuration guide](./Global-Configuration.md). Missing metrics or scope labels make the policy wait rather than query unscoped data.

## How trials are scored

The controller compares each candidate with a fresh measurement of the selected configuration. Throughput contributes `ln(candidate / reference)`; latency contributes `ln(reference / candidate)`. It multiplies each value by its objective weight, normalizes by the total weight, then scales the score by 100. A candidate must meet `minScoreGain`, pass absolute latency limits, fit the regression budgets, and use comparable traffic. If the selected configuration violates a hard latency limit, a candidate that restores compliance can be accepted without score gain. Other constraints and regression budgets still apply.

Regression budgets use the fixed measurement from the start of the search. This stops several small regressions from accumulating past the configured budget. A new search captures a new reference.

Traffic comparison uses mean prompt/output lengths, completed-request rate, running/waiting request gauges, and the fraction of the window with waiting requests. When the waiting fraction meets `minBackloggedFraction`, completed-request rate need not stay equal because a higher completion rate can be the improvement. Below that threshold, the controller treats traffic as demand-limited, requires comparable request rates, and omits throughput from the score while keeping the original weight denominator. A material change in prompt/output mix or backlog eligibility defers candidate scoring. These metrics are demand proxies, not proof of saturation or identical requests. Run controlled tests before relying on the comparison.

## Monitoring and status

After a search settles, the controller measures the selected configuration again and uses that as the monitoring reference. It starts a new search after a sustained traffic change, sustained latency-limit violation, periodic rescan deadline, or policy change. Invalid observations and missed monitoring checks reset sustained-change detection. Candidate changes respect cooldown; a required rollback does not.

`status.phase` is policy-wide. `Monitoring` means the policy is settled and will keep checking, not that it has stopped adapting. Root `Ready=True` means the selected configuration has a valid measurement and meets its constraints. `status.selectedConfiguration` and `status.originalConfiguration` are authoritative complete argument snapshots. `status.parameters` is a derived summary, and `status.lastDecision` retains only the latest candidate outcome.

## Rollback, deletion, and upgrade

A rejected trial restores the prior complete configuration. Deleting the policy restores the original explicit argument, or removes it if the argument was originally absent. Wait for replacement pods and finalizer removal before considering cleanup complete. Do not remove the finalizer manually.

The status and spec changed incompatibly from the previous experimental API. Before upgrading, save policy specs, delete the policies with the old controller running, and wait for argument restoration, owned `ContainerArgsPolicy` deletion, and finalizer removal. Upgrade the controller and CRD only after cleanup finishes, then translate and recreate the saved policies. Do not clear status or finalizers by hand.

The tuner measures performance at a fixed allocation. It does not prove that fewer GPUs or replicas are safe, and it does not claim a cost reduction.
