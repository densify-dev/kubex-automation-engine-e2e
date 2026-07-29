# Cluster Compaction Policies

## What is this feature?

Active bin-packing (compaction) consolidates your cluster's workloads onto fewer nodes so that underutilised nodes can be freed for scale-down or cost reduction. Without compaction, workloads that were spread across nodes during a traffic spike stay spread even after load drops — the cluster never self-consolidates.

`ClusterCompactionPolicy` is the Kubex API for declaring compaction intent. Each policy:

1. **Selects workloads** — by namespace, label selector, and workload type.
2. **Assigns a scheduler to new Pods** — the admission webhook directs newly created Pods for matching workloads to the Kubex compaction scheduler (or a nominated external scheduler), which uses a `MostAllocated` priority to pack them onto already-busy nodes.
3. **Creates a descheduler** — the controller provisions a dedicated descheduler CronJob for the policy. Each run evicts pods that are on underutilised nodes, triggering re-scheduling onto denser nodes.
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

The controller reconciles the policy, labels matched workloads, and creates the per-policy descheduler CronJob automatically. No further action is needed.

### 3. Verify

```bash
# Check the policy status
kubectl get clustercompactionpolicies -o wide

# Check the managed workloads have been labelled
kubectl get deployments -A -l scheduling.kubex.ai/compaction-policy=my-compaction-policy

# Check the per-policy descheduler CronJob
kubectl get cronjob -n kubex kubex-compaction-descheduler-my-compaction-policy
```

## Model

- Helm installs the shared compaction scheduler support objects only.
- The controller generates the scheduler config and one descheduler CronJob/ConfigMap pair per active descheduler policy.
- `ClusterCompactionPolicy` chooses the effective policy per workload.
- Kubex-managed compaction workloads use scheduler name `kubex-compaction-scheduler`; external scheduler mode uses the configured external scheduler name instead.
- The scheduler image tag auto-defaults from the target cluster version unless explicitly overridden.

## Reference Rules

- `ClusterCompactionPolicy` is cluster-scoped.
- Multiple policies may match the same workload space.
- If multiple policies overlap, the controller resolves a single effective policy using `weight`, then scope specificity, then age, then name.
- Each effective policy gets a deterministic workload label: `scheduling.kubex.ai/compaction-policy=<policy-name>`.
- The shared suppression label is `scheduling.kubex.ai/compaction-suppressed=true`.
- The controller writes a versioned `scheduling.kubex.ai/compaction-intent` annotation to participating workload metadata. The Pod mutating admission webhook resolves the workload owner, reads that annotation, and sets compaction labels and `spec.schedulerName` on each new Pod. It does not modify the workload pod template or existing Pods.
- When descheduling is enabled, the controller also writes a Pod runtime-hook recommendation. Existing Running Pods that missed the required admission state are replaced through the Kubernetes Eviction API one at a time per workload owner; their replacements receive compaction state during admission. PodDisruptionBudgets remain enforced.

Example workload intent:

```yaml
metadata:
  annotations:
    scheduling.kubex.ai/compaction-intent: '{"apiVersion":"scheduling.kubex.ai/v1alpha1","policyName":"platform-active-binpacking","schedulerName":"kubex-compaction-scheduler","schedulerProfileName":"compaction-most-allocated","deschedulerEnabled":true,"suppressed":false}'
```

## Workload type support

Scheduler assignment depends on whether admission can resolve an annotated supported owner. Descheduler support remains narrower because safe eviction and recreation semantics vary by workload controller.

| Workload type | Policy label | Scheduler assignment | Descheduler eviction | Notes |
|---|---|---|---|---|
| `Deployment` | ✅ | ✅ | ✅ | Existing Pods remain untouched; new or naturally replaced Pods are mutated at admission. |
| `StatefulSet` | ✅ | ✅ | ✅ | Existing Pods remain untouched; new or naturally replaced Pods are mutated at admission. |
| `DaemonSet` | ✅ | ✅ | — | SchedulerName is assigned at Pod admission; Pods are not evictable by `HighNodeUtilization` by default (`evictDaemonSetPods: false`). |
| `CronJob` | ✅ | ✅ | — | The webhook follows Pod → Job → CronJob and applies intent from the annotated CronJob. |
| `Rollout` (Argo) | ✅ | ✅ | — | The webhook resolves the Rollout owner; descheduler eviction depends on your Rollout strategy. |
| `Job` | ✅ | Limited | — | The initial Pod can race owner annotation; later retry Pods are mutated after intent is present. |
| `AnalysisRun` (Argo) | ✅ | Limited | — | The initial Pod can race owner annotation; later Pods are mutated after intent is present. |
| `StrimziPodSet` | ✅ | ✅ | — | Newly created or replacement Pods inherit intent through the owner reference. |
| `Model` (KubeAI) | ✅ | ✅ | — | Newly created or replacement model Pods inherit intent through the owner reference. |

**Known limitations**

- Scheduler assignment requires the Pod mutating admission webhook. Its failure policy is `Ignore`, so a Pod is admitted with its existing schedulerName when the webhook is unavailable.
- KAI GPU scheduling takes precedence when the same Pod receives both KAI resize actions and compaction intent. The webhook records this override in the controller log.
- Existing Pods are never rewritten to change scheduler assignment. When descheduling is enabled, admission-noncompliant Pods may be evicted and recreated; otherwise they retain their current scheduler and labels until naturally replaced.
- The compaction controller never modifies pod-template metadata or spec. Top-level workload metadata changes do not trigger a rollout.
- Admission uses the nearest annotated supported owner and falls back up the owner chain, such as from an unannotated Job to its annotated CronJob.
- `Job` and `AnalysisRun` scheduler assignment is best-effort because their controllers may create the initial Pod before compaction intent is reconciled onto the owner.

The runtime hook honors `descheduler.nodeSelector`, `defaultEvictor.evictSystemCriticalPods`, `defaultEvictor.evictLocalStoragePods`, `defaultEvictor.ignorePvcPods`, and `defaultEvictor.evictDaemonSetPods`. It deliberately ignores `defaultEvictor.nodeFit`, `defaultEvictor.labelSelector`, the `maxNoOfPodsToEvict*` limits, `interval`, and `highNodeUtilization`; those settings govern descheduler balancing runs rather than admission convergence. Unscheduled, terminal, deleting, protected-namespace, and suppressed Pods are not replaced.

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
| `spec.descheduler.interval` | `*/30 * * * *` | Five-field CronJob schedule for each one-shot run (e.g. `*/30 * * * *`, `0 */2 * * *`). Duration values such as `1m` and `30s` are invalid. |
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
- The controller also reads `COMPACTION_DESCHEDULER_*` settings from its Deployment and creates one descheduler CronJob/ConfigMap per active descheduler policy using the fixed `kubex-compaction-descheduler` prefix.
- A policy with `spec.descheduler.enabled: true` but no managed workloads gets a suspended descheduler CronJob.
- OpenShift compatibility settings apply to the Kubex-managed scheduler Deployment; external scheduler mode does not create a Kubex scheduler workload.
