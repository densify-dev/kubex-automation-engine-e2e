# StaticPolicy + AutomationStrategy examples

This directory has example bundles:

1) `simple.yaml`  
   - Namespaced AutomationStrategy (`sample-automation-strategy`) that allows *only* in-place resizing (no eviction).  
   - An StaticPolicy targeting `app: rightsizing-demo` and a demo Deployment to see the annotations applied.

2) `matrix.yaml`  
   - Multiple AutomationStrategy/StaticPolicy pairs to cover common scenarios:
     - **sp-inplace-only / ar-inplace-only**: in-place only, no eviction.
     - **sp-eviction-only / ar-eviction-only**: eviction only, no in-place.
    - **sp-cpu-only / ar-cpu-only**: CPU actions enabled, memory disabled.
    - **sp-memory-only / ar-memory-only**: Memory actions enabled, CPU disabled.

3) `namespaced-and-cluster.yaml`  
   - Demonstrates both namespaced and cluster-scoped policies in one bundle:
     - `StaticPolicy` in `default` using `AutomationStrategy` (`sample-automation-strategy`).
     - `ClusterStaticPolicy` using a `ClusterAutomationStrategy` (`sample-cluster-automation-strategy`).
     - Two demo Deployments (one in `default`, one in the `example` namespace) to show cross-namespace behavior.

4) `per-container.yaml`  
   - Demonstrates `resources.containers` with per-container requests/limits and an optional `resources.all` fallback.

5) `with-resource-quota.yaml`  
   - Adds a namespace ResourceQuota that blocks the projected resize, showing the resource-quota precheck behavior.

6) `multi-container-filtered.yaml`  
   - Multi-container Deployment where one container violates a LimitRange and gets filtered while the other container passes.

7) `multi-container.yaml`
   - Static-policy counterpart to `examples/proactivepolicy/multi-container.yaml`.
   - Encodes the same intended per-container memory request targets directly in `resources.containers`.

8) `enablement-directions.yaml`  
   - Exercises AutomationStrategy enablement directions for requests and limits, including set-from-unspecified behavior.

9) `with-pdb-multi-replica.yaml`  
   - Multi-replica Deployment plus PodDisruptionBudget to exercise eviction-based resizing behavior under PDB constraints.

10) `same-order-weight-precedence.yaml`  
    - PolicyEvaluation gives `StaticPolicy` and `ClusterStaticPolicy` the same priority; weight determines the winner.

11) `namespaced-and-cluster-same-weight.yaml`  
    - Demonstrates the same namespaced/cluster policy pair with equal weights.
    - The older policy wins, so `creationTimestamp` determines the effective policy.

12) `wildcard-weight-precedence.yaml`  
    - Two `StaticPolicy` resources with different weights: a wildcard `*` policy should win over a lower-weight per-container policy.

13) `with-keda-hpa-filter.yaml`
    - Assumes KEDA is installed.
    - Deploys a `ScaledObject` targeting the demo workload so KEDA creates/manages an HPA with non-resource metrics.
    - Exercises KEDA-aware HPA filtering to block CPU and memory resize actions.

14) `with-resource-bounds.yaml`
    - Adds `floor`/`ceiling` bounds under `spec.enablement` in `AutomationStrategy`.
    - Exercises desired-value clamping for CPU/memory requests/limits.

15) `cronjob.yaml`
    - Demonstrates `StaticPolicy` targeting `CronJob` workloads via `scope.workloadTypes`.
    - Includes a sample CronJob selected by label to show static request/limit recommendations.

16) `request-exceeds-limit-after-limit-filter.yaml`
    - Reproduces a filtered-limit scenario:
      - Pod-level `LimitRange` max filters out desired CPU limit action (`700m`).
      - Desired CPU request action (`300m`) remains.
      - Final pre-check blocks resize because request would exceed current effective limit (`200m`).

17) `in-place-memory-decrease-success.yaml`
    - Happy-path validation for in-place memory downsize fallback handling.
    - Uses a Deployment with `256Mi` memory request/limit and a `StaticPolicy` that targets `128Mi`.
    - Intended to trigger Kubernetes API rejection for memory limit downsize unless resize policy allows restart, then fall back to eviction.

18) `in-place-memory-decrease-fail.yaml`
    - Negative-path validation for non-memory resize API rejection behavior.
    - Uses `ephemeral-storage` request/limit targets so the in-place resize path hits a different API rejection than the memory-downsize-specific error.
    - Intended to verify the controller logs the generic in-place failure fallback path.

19) `with-skip-containers.yaml`
    - Single `AutomationStrategy` + single `StaticPolicy` bundled with 4 minimal Deployments.
    - Cases covered:
      - `container-skip-owner-level`: owner annotation `"app"`; `app` filtered, `sidecar` proceeds.
      - `container-skip-pod-level`: pod annotation `" app, sidecar, app "`; trim + dedupe, both containers filtered.
      - `container-skip-pod-overrides-owner`: owner `"app"`, pod `"sidecar"`; pod wins, only `sidecar` filtered.
      - `container-skip-empty-pod-clears-owner-fallback`: owner `"app"`, pod `" , "`; present-but-empty pod value clears owner fallback.
    - Apply with:
      - `kubectl apply -f examples/staticpolicy/with-skip-containers.yaml`

20) `model.yaml`
    - Demonstrates `StaticPolicy` targeting KubeAI `kubeai.org/v1` `Model` workloads.
    - Includes sample `Model`; when KubeAI creates model-owned pods, recommendations anchor on `Model` and flow to those pods.
    - Requires KubeAI CRD installed in cluster.

21) `multi-policy-container-scope.yaml`
    - Uses one AutomationStrategy and two StaticPolicies against one three-container Deployment.
    - Each policy scopes to a different container and uses `resources.containers."*"`.
    - Requires `GlobalConfiguration.spec.multiPolicyContainerRightsizingEnabled: true`; the unscoped `metrics` container keeps its initial resources.

Apply examples:

```sh
kubectl apply -f examples/staticpolicy/simple.yaml
kubectl apply -f examples/staticpolicy/matrix.yaml
kubectl apply -f examples/staticpolicy/namespaced-and-cluster.yaml
kubectl apply -f examples/staticpolicy/per-container.yaml
kubectl apply -f examples/staticpolicy/with-resource-quota.yaml
kubectl apply -f examples/staticpolicy/multi-container-filtered.yaml
kubectl apply -f examples/staticpolicy/multi-container.yaml
kubectl apply -f examples/staticpolicy/multi-policy-container-scope.yaml
kubectl apply -f examples/staticpolicy/enablement-directions.yaml
kubectl apply -f examples/staticpolicy/with-pdb-multi-replica.yaml
kubectl apply -f examples/staticpolicy/same-order-weight-precedence.yaml
kubectl apply -f examples/staticpolicy/namespaced-and-cluster-same-weight.yaml
kubectl apply -f examples/staticpolicy/wildcard-weight-precedence.yaml
kubectl apply -f examples/staticpolicy/with-keda-hpa-filter.yaml
kubectl apply -f examples/staticpolicy/with-resource-bounds.yaml
kubectl apply -f examples/staticpolicy/cronjob.yaml
kubectl apply -f examples/staticpolicy/request-exceeds-limit-after-limit-filter.yaml
kubectl apply -f examples/staticpolicy/in-place-memory-decrease-success.yaml
kubectl apply -f examples/staticpolicy/in-place-memory-decrease-fail.yaml
kubectl apply -f examples/staticpolicy/with-skip-containers.yaml
kubectl apply -f examples/staticpolicy/model.yaml
```

The `multi-policy-container-scope.yaml` example requires:

```text
GlobalConfiguration.spec.multiPolicyContainerRightsizingEnabled: true
```

The example does not include a `GlobalConfiguration` because Helm manages the cluster singleton. Record the current value, enable the flag by patching the existing object, and apply the example:

```sh
previous_multi_policy_flag="$(kubectl get globalconfiguration global-config -o jsonpath='{.spec.multiPolicyContainerRightsizingEnabled}')"
test -n "$previous_multi_policy_flag" || previous_multi_policy_flag=false
kubectl patch globalconfiguration global-config --type=merge \
  -p '{"spec":{"multiPolicyContainerRightsizingEnabled":true}}'
kubectl apply -f examples/staticpolicy/multi-policy-container-scope.yaml
```

After reconciliation, the Deployment should have these values:

| Container | Requests | Limits |
| --- | --- | --- |
| `app` | `cpu: 250m`, `memory: 256Mi` | `cpu: 500m`, `memory: 512Mi` |
| `sidecar` | `cpu: 150m`, `memory: 192Mi` | `cpu: 300m`, `memory: 384Mi` |
| `metrics` | `cpu: 25m`, `memory: 32Mi` | `cpu: 50m`, `memory: 64Mi` |

`metrics` is outside both policy scopes, so its initial values stay unchanged. Inspect the resources with:

```sh
kubectl get deployment multi-policy-container-scope-demo -n default \
  -o jsonpath='{range .spec.template.spec.containers[*]}{.name}: requests={.resources.requests} limits={.resources.limits}{"\n"}{end}'
```

With the flag enabled, the Deployment has separate hashed annotation keys for each policy. Each key starts with `static.rightsizing.kubex.ai/h` and ends with either `-desired-resource-requests` or `-desired-resource-limits`:

```sh
kubectl get deployment multi-policy-container-scope-demo -n default -o yaml \
  | grep -E 'static\.rightsizing\.kubex\.ai/h.*desired-resource-(requests|limits)'
```

Clean up the example, then restore the value recorded before the example. If the commands run in separate shells, replace `$previous_multi_policy_flag` with the recorded `true` or `false` value:

```sh
kubectl delete -f examples/staticpolicy/multi-policy-container-scope.yaml
kubectl patch globalconfiguration global-config --type=merge \
  -p "{\"spec\":{\"multiPolicyContainerRightsizingEnabled\":${previous_multi_policy_flag}}}"
```

Then inspect workloads with the matching labels (e.g., `kubectl get deploy -n default -o yaml`) to see desired request/limit annotations and the resolved rule metadata.

For the KEDA example, verify the KEDA-managed HPA exists:

```sh
kubectl get hpa -n default -l scaledobject.keda.sh/name=rightsizing-demo-keda-hpa-filter
```
