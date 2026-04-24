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

11) `wildcard-weight-precedence.yaml`  
   - Two `StaticPolicy` resources with different weights: a wildcard `*` policy should win over a lower-weight per-container policy.

12) `with-keda-hpa-filter.yaml`
   - Assumes KEDA is installed.
   - Deploys a `ScaledObject` targeting the demo workload so KEDA creates/manages an HPA with non-resource metrics.
   - Exercises KEDA-aware HPA filtering to block CPU and memory resize actions.

13) `with-resource-bounds.yaml`
   - Adds `floor`/`ceiling` bounds under `spec.enablement` in `AutomationStrategy`.
   - Exercises desired-value clamping for CPU/memory requests/limits.

14) `cronjob.yaml`
   - Demonstrates `StaticPolicy` targeting `CronJob` workloads via `scope.workloadTypes`.
   - Includes a sample CronJob selected by label to show static request/limit recommendations.

15) `request-exceeds-limit-after-limit-filter.yaml`
   - Reproduces a filtered-limit scenario:
     - Pod-level `LimitRange` max filters out desired CPU limit action (`700m`).
     - Desired CPU request action (`300m`) remains.
     - Final pre-check blocks resize because request would exceed current effective limit (`200m`).

16) `in-place-memory-decrease-success.yaml`
   - Happy-path validation for in-place memory downsize fallback handling.
   - Uses a Deployment with `256Mi` memory request/limit and a `StaticPolicy` that targets `128Mi`.
   - Intended to trigger Kubernetes API rejection for memory limit downsize unless resize policy allows restart, then fall back to eviction.

17) `in-place-memory-decrease-fail.yaml`
   - Negative-path validation for non-memory resize API rejection behavior.
   - Uses `ephemeral-storage` request/limit targets so the in-place resize path hits a different API rejection than the memory-downsize-specific error.
   - Intended to verify the controller logs the generic in-place failure fallback path.

Apply examples:

```sh
kubectl apply -f examples/staticpolicy/simple.yaml
kubectl apply -f examples/staticpolicy/matrix.yaml
kubectl apply -f examples/staticpolicy/namespaced-and-cluster.yaml
kubectl apply -f examples/staticpolicy/per-container.yaml
kubectl apply -f examples/staticpolicy/with-resource-quota.yaml
kubectl apply -f examples/staticpolicy/multi-container-filtered.yaml
kubectl apply -f examples/staticpolicy/multi-container.yaml
kubectl apply -f examples/staticpolicy/enablement-directions.yaml
kubectl apply -f examples/staticpolicy/with-pdb-multi-replica.yaml
kubectl apply -f examples/staticpolicy/same-order-weight-precedence.yaml
kubectl apply -f examples/staticpolicy/wildcard-weight-precedence.yaml
kubectl apply -f examples/staticpolicy/with-keda-hpa-filter.yaml
kubectl apply -f examples/staticpolicy/with-resource-bounds.yaml
kubectl apply -f examples/staticpolicy/cronjob.yaml
kubectl apply -f examples/staticpolicy/request-exceeds-limit-after-limit-filter.yaml
kubectl apply -f examples/staticpolicy/in-place-memory-decrease-success.yaml
kubectl apply -f examples/staticpolicy/in-place-memory-decrease-fail.yaml
```

Then inspect workloads with the matching labels (e.g., `kubectl get deploy -n default -o yaml`) to see desired request/limit annotations and the resolved rule metadata.

For the KEDA example, verify the KEDA-managed HPA exists:

```sh
kubectl get hpa -n default -l scaledobject.keda.sh/name=rightsizing-demo-keda-hpa-filter
```
