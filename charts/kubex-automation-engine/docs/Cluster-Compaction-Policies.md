# Cluster Compaction Policies

`ClusterCompactionPolicy` defines which workloads participate in active bin-packing and how Kubex binds them to the dedicated compaction scheduler and descheduler.

## Model

- Helm installs the Kubex-managed compaction scheduler and descheduler workloads only.
- The controller generates the scheduler and descheduler configs from active policies.
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

## Field Reference

| Field | Default | Description |
| --- | --- | --- |
| `spec.scope.labelSelector` | none | Kubernetes label selector for matching workloads. |
| `spec.scope.workloadTypes` | `[Deployment, StatefulSet, CronJob, Rollout, Job, AnalysisRun, DaemonSet, Model]` | Workload kinds this policy applies to. |
| `spec.scope.namespaceSelector.operator` | none | Namespace selector operator: `In` or `NotIn`. |
| `spec.scope.namespaceSelector.values` | none | Namespace patterns to include or exclude (supports `*` wildcards, e.g. `prod-*`). |
| `spec.enabled` | `true` | Controls whether this policy participates in selection and enforcement. |
| `spec.scheduler.useKubexScheduler` | `true` | Controls whether matching workloads use the Kubex-managed compaction scheduler. |
| `spec.scheduler.externalSchedulerName` | none | External scheduler name used when `useKubexScheduler` is false. |
| `spec.descheduler.enabled` | `true` | Controls whether matching workloads participate in descheduler-driven compaction. |
| `spec.descheduler.profileName` | none | Descheduler profile name generated for this policy. |
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
    profileName: compaction
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
- The descheduler policy uses the effective policy label selector implicitly by default; `spec.descheduler.defaultEvictor.labelSelector` replaces that implicit selector when set.
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
- The compaction scheduler ConfigMap is labeled with `app.kubernetes.io/component: compaction-scheduler` so the controller can patch it without knowing the Helm release name.
- OpenShift compatibility settings apply to the Kubex-managed scheduler Deployment; external scheduler mode does not create a Kubex scheduler workload.
