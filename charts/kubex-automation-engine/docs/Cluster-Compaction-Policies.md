# Cluster Compaction Policies

## What is this feature?

Active bin-packing (compaction) consolidates your cluster's workloads onto fewer nodes so that underutilised nodes can be freed for scale-down or cost reduction. Without compaction, workloads that were spread across nodes during a traffic spike stay spread even after load drops — the cluster never self-consolidates.

`ClusterCompactionPolicy` is the Kubex API for declaring compaction intent. Each policy:

1. **Selects workloads** — by namespace, label selector, and workload type.
2. **Assigns a scheduler** — workloads matching an active policy are re-pointed to the Kubex compaction scheduler (or a nominated external scheduler), which uses a `MostAllocated` priority to pack new pods onto already-busy nodes.
3. **Creates a descheduler** — the controller provisions a dedicated descheduler Deployment for the policy. The descheduler periodically evicts pods that are on underutilised nodes, triggering re-scheduling onto denser nodes.
4. **Suppresses eviction loops** — if a workload keeps being evicted without successfully settling, Kubex detects the loop fingerprint and suppresses it temporarily, preventing thrashing.

## How to enable

### 1. Install the Helm chart with compaction support

```yaml
# values.yaml
compactionScheduler:
  enabled: true          # deploy the shared MostAllocated scheduler

compactionDescheduler:
  enabled: true          # deploy shared RBAC / service account for per-policy deschedulers
```

Helm installs only the ServiceAccount and RBAC for the compaction scheduler. The `kubex-compaction-scheduler` Deployment and its ConfigMap are fully owned by the controller: the Deployment is created on first reconcile and kept in sync thereafter (image tag auto-updated to match the cluster Kubernetes version; ConfigMap regenerated whenever policies change). No GitOps drift-ignore configuration is required.

### 2. Create a ClusterCompactionPolicy

```yaml
apiVersion: rightsizing.kubex.ai/v1alpha1
kind: ClusterCompactionPolicy
metadata:
  name: my-compaction-policy
spec:
  scope:
    namespaceSelector:
      operator: In
      values:
        - production
    workloadTypes:
      - Deployment
      - StatefulSet
  enabled: true
  scheduler:
    useKubexScheduler: true
  descheduler:
    enabled: true
    maxNoOfPodsToEvictPerNode: 1
    maxNoOfPodsToEvictTotal: 5
```

The controller reconciles the policy, labels matched workloads, and creates the per-policy descheduler Deployment automatically. No further action is needed.

### 3. Verify

```bash
# Check the policy status
kubectl get clustercompactionpolicies -o wide

# Check the managed workloads have been labelled
kubectl get deployments -A -l scheduling.kubex.ai/compaction-policy=my-compaction-policy

# Check the per-policy descheduler Deployment
kubectl get deployment -n kubex kubex-compaction-descheduler-my-compaction-policy
```

## Model

- Helm installs the shared compaction scheduler support objects only.
- The controller generates the scheduler config and one descheduler Deployment/ConfigMap pair per active descheduler policy.
- `ClusterCompactionPolicy` chooses the effective policy per workload.
- Kubex-managed compaction workloads use scheduler name `kubex-compaction-scheduler`; external scheduler mode uses the configured external scheduler name instead.
- The scheduler image tag auto-defaults from the target cluster version unless explicitly overridden.

## Reference Rules

- `ClusterCompactionPolicy` is cluster-scoped.
- Multiple policies may match the same workload space.
- If multiple policies overlap, the controller resolves a single effective policy using `weight`, then scope specificity, then age, then name.
- Each effective policy gets a deterministic workload label: `scheduling.kubex.ai/compaction-policy=<policy-name>`.
- The shared suppression label is `scheduling.kubex.ai/compaction-suppressed=true`.
- The controller also writes `spec.template.spec.schedulerName=kubex-compaction-scheduler` for participating workloads.

## Workload type support

Not every workload type receives the same level of compaction support. The table below is authoritative — types not listed as "full" receive only the compaction-policy label on the workload object itself and are not enrolled in scheduler assignment or descheduler eviction.

| Workload type | Policy label | Scheduler assignment | Descheduler eviction | Notes |
|---|---|---|---|---|
| `Deployment` | ✅ | ✅ | ✅ | Fully supported. |
| `StatefulSet` | ✅ | ✅ | ✅ | Fully supported. |
| `DaemonSet` | ✅ | ✅ | — | Pods are not evictable by `HighNodeUtilization` by default (`evictDaemonSetPods: false`). |
| `CronJob` | ✅ | ✅ | — | Scheduler is assigned to future Job runs via the job template; existing Jobs are not mutated. |
| `Rollout` (Argo) | ✅ | ✅ | — | Pod template is patched; descheduler eviction depends on your Rollout strategy. |
| `Job` | ✅ | — | — | Pod templates are immutable after creation; the controller labels the Job object only. |
| `AnalysisRun` (Argo) | ✅ | — | — | No pod template access; object label only. |
| `StrimziPodSet` | ✅ | — | — | No pod template access; object label only. |
| `Model` (KubeAI) | ✅ | — | — | No pod template access; object label only. |

**Known limitations**

- `Job` pod templates cannot be patched after the Job is created. If you include `Job` in `workloadTypes`, the controller labels the Job object for visibility but does not assign a scheduler.
- `AnalysisRun`, `StrimziPodSet`, and `Model` carry the `scheduling.kubex.ai/compaction-policy` label for identification, but pods spawned by these workloads are not directed to the compaction scheduler and are not considered by the descheduler's label filter. Use `Deployment` and `StatefulSet` for end-to-end compaction.

## Field Reference

| Field | Default | Description |
| --- | --- | --- |
| `spec.scope.labelSelector` | none | Kubernetes label selector for matching workloads. |
| `spec.scope.workloadTypes` | `[Deployment, StatefulSet, CronJob, Rollout, Job, AnalysisRun, DaemonSet, Model]` | Workload kinds this policy applies to. See the workload type support table above for per-type capabilities. |
| `spec.scope.namespaceSelector.operator` | none | Namespace selector operator: `In` or `NotIn`. |
| `spec.scope.namespaceSelector.values` | none | Namespace patterns to include or exclude (supports `*` wildcards, e.g. `prod-*`). |
| `spec.enabled` | `true` | Controls whether this policy participates in selection and enforcement. |
| `spec.scheduler.useKubexScheduler` | `true` | Controls whether matching workloads use the Kubex-managed compaction scheduler. |
| `spec.scheduler.externalSchedulerName` | none | External scheduler name used when `useKubexScheduler` is false. |
| `spec.descheduler.enabled` | `true` | Controls whether matching workloads participate in descheduler-driven compaction. |
| `spec.descheduler.nodeSelector` | none | Narrows the descheduler policy to nodes with matching labels. |
| `spec.descheduler.maxNoOfPodsToEvictPerNode` | `1` | Max descheduler evictions per node for this policy. |
| `spec.descheduler.maxNoOfPodsToEvictPerNamespace` | `1` | Max descheduler evictions per namespace for this policy. |
| `spec.descheduler.maxNoOfPodsToEvictTotal` | `1` | Max total descheduler evictions per run for this policy. |
| `spec.descheduler.defaultEvictor.nodeFit` | `true` | Require evicted pods to appear to fit elsewhere. |
| `spec.descheduler.defaultEvictor.evictSystemCriticalPods` | `false` | Keep system-critical pods out of compaction by default. |
| `spec.descheduler.defaultEvictor.evictLocalStoragePods` | `false` | Avoid evicting local-storage pods by default. |
| `spec.descheduler.defaultEvictor.ignorePvcPods` | `true` | Ignore PVC-backed pods by default. |
| `spec.descheduler.defaultEvictor.evictDaemonSetPods` | `false` | Avoid evicting DaemonSet pods by default. |
| `spec.descheduler.defaultEvictor.labelSelector` | none | Overrides the implicit policy-label selector when set. |
| `spec.descheduler.highNodeUtilization.numberOfNodes` | `1` | Minimum underutilized nodes required before acting. |
| `spec.descheduler.highNodeUtilization.thresholds.cpu` | `25` | CPU utilization threshold for `HighNodeUtilization`. |
| `spec.descheduler.highNodeUtilization.thresholds.memory` | `25` | Memory utilization threshold for `HighNodeUtilization`. |
| `spec.descheduler.highNodeUtilization.thresholds.pods` | `25` | Pod utilization threshold for `HighNodeUtilization`. |
| `spec.descheduler.interval` | `*/30 * * * *` | Cron schedule for how often the descheduler runs (e.g. `*/30 * * * *`, `0 */2 * * *`). |
| `spec.descheduler.loopDetectionWindow` | `15m` | Rolling window for counting repeated same-fingerprint evictions. |
| `spec.descheduler.loopDetectionThreshold` | `3` | Number of observations within the window before suppression triggers. |
| `spec.descheduler.suppressionDuration` | `=loopDetectionWindow` | How long the suppressed label stays on the workload. |
| `spec.weight` | `0` | Higher weight wins when multiple compaction policies match. |

## Example

```yaml
apiVersion: rightsizing.kubex.ai/v1alpha1
kind: ClusterCompactionPolicy
metadata:
  name: platform-active-binpacking
spec:
  scope:
    labelSelector:
      matchLabels:
        team: platform
    workloadTypes:
      - Deployment
      - StatefulSet
    namespaceSelector:
      operator: NotIn
      values:
        - kube-system
        - kubex
  enabled: true
  scheduler:
    useKubexScheduler: true
    externalSchedulerName: ""
  descheduler:
    enabled: true
    nodeSelector:
      matchLabels:
        node-role.kubernetes.io/busy: "true"
    maxNoOfPodsToEvictPerNode: 1
    maxNoOfPodsToEvictPerNamespace: 1
    maxNoOfPodsToEvictTotal: 1
    defaultEvictor:
      nodeFit: true
      evictSystemCriticalPods: false
      evictLocalStoragePods: false
      ignorePvcPods: true
      evictDaemonSetPods: false
      labelSelector:
        matchLabels:
          scheduling.kubex.ai/binpack: "true"
    highNodeUtilization:
      numberOfNodes: 1
      thresholds:
        cpu: 25
        memory: 25
        pods: 25
  weight: 50
status:
  summary:
    enabled: true
    effectivePolicyLabel: scheduling.kubex.ai/compaction-policy=platform-active-binpacking
    schedulerName: kubex-compaction-scheduler
    schedulerProfileName: compaction-most-allocated
    deschedulerProfileName: compaction
```

## Notes

- Set `spec.enabled: false` when you want to keep the object without letting it participate in selection.
- The scheduler is a dedicated Kubex-managed scheduler when `spec.scheduler.useKubexScheduler=true`; otherwise Kubex only targets the named external scheduler.
- Scheduler modes:
  - `useKubexScheduler: true` -> Kubex-managed scheduler `kubex-compaction-scheduler`
  - `useKubexScheduler: false` and `externalSchedulerName: <name>` -> external scheduler targeting only
  - `useKubexScheduler: false` and `externalSchedulerName: ""` -> descheduler-only compaction
- Policy status records whether the policy is enabled, the resolved scheduler name, and the expected scheduler/descheduler readiness state.
- The descheduler policy uses the effective policy label selector implicitly by default; `spec.descheduler.defaultEvictor.labelSelector` adds to that selector and `spec.descheduler.nodeSelector` narrows the policy to matching nodes.
- Starting image versions in this implementation:
  - scheduler: `registry.k8s.io/kube-scheduler:<cluster-version>` by default
  - descheduler: `registry.k8s.io/descheduler/descheduler:v0.36.0`
- Scheduler image skew policy:
  - exact cluster-version match is the steady state
  - one minor older is allowed temporarily for upgrades
  - newer-than-cluster and more-than-one-minor-older are rejected during Helm render
- Use `compactionScheduler.kubernetesVersionOverride` only when the cluster version cannot be discovered reliably during template rendering.
- Suppression only blocks descheduler disruption; it does not disable workload participation in the scheduler.
- The controller now also reads `COMPACTION_SCHEDULER_IMAGE_REPOSITORY` from its Deployment, then reconciles the scheduler ConfigMap and image tag at runtime when active policies use the Kubex-managed scheduler.
- External scheduler mode is workload targeting only; Kubex does not manage scheduler runtime/config for those policies.
- The compaction scheduler ConfigMap is a fixed name: `kubex-compaction-scheduler-config`.
- The controller also reads `COMPACTION_DESCHEDULER_*` settings from its Deployment and creates one descheduler Deployment/ConfigMap per active descheduler policy using the fixed `kubex-compaction-descheduler` prefix.
- A policy with `spec.descheduler.enabled: true` but no managed workloads does not get a descheduler Deployment.
- OpenShift compatibility settings apply to the Kubex-managed scheduler Deployment; external scheduler mode does not create a Kubex scheduler workload.
