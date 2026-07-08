# Automation Controller E2E Test Framework

Pytest-based end-to-end tests for the `automation-controller` running on a self-managed local kind cluster.

## Prerequisites

```bash
pip install pytest pytest-timeout kubernetes
```

Host tools required on the machine that runs the suite:

```bash
kind
kubectl
helm
docker
make
```

The framework assumes it can create and manage its own Kind cluster for a test run. By default it bootstraps:

- a Kind cluster matching `--kube-context`
- `metrics-server` for HPA coverage
- `KEDA` for KEDA-managed HPA coverage
- `VPA` for VPA-backed example coverage

GPU coverage can be folded into the standard suite by enabling `GPU_SUITE=true` and pointing `GPU_KIND_CONFIG` at `test/e2e/features/gpu/kind-config.yaml`.

The CI matrix runs two variants, and both include GPU coverage: **v1.35.0** with the full stack and **v1.32.0** with metrics-server only (`WITH_KEDA=false WITH_VPA=false`).

GPU coverage runs through the standard suite when `GPU_SUITE=true`; there is no separate GPU-only lane.

Controller installation is handled by the Python bootstrap module. It installs the Helm charts using chart defaults by default, and only generates image override values when you pass `--controller-image-repository` and `--controller-image-tag`.

By default the runners also load [examples/recommendations.json](examples/recommendations.json) into a `recommendations` `ConfigMap` and enable the chart's `localRecommendations` mode so recommendation-dependent tests exercise real data instead of just status fields.

VPA is installed by default so the full suite can cover VPA-backed examples and filters. Set `WITH_VPA=false` when you explicitly want a leaner cluster bootstrap.

If you already have a cluster and controller running, pass `--skip-kind-bootstrap` to disable framework-managed bootstrap.

The framework can also target specific Kubernetes versions by selecting the Kind node image with `--kind-node-image`. This matters for resize behavior because Kubernetes `1.35+` supports in-place resize directly, while pre-`1.35` clusters may fall back to eviction-driven behavior.

After bootstrap, the suite expects these controller-managed resources to exist:

| Resource | Name | Scope |
|---|---|---|
| `GlobalConfiguration` | `global-config` | cluster |
| `PolicyEvaluation` | `policy-evaluation` | cluster |

## Usage

```bash
# Basic run against the default test cluster
./run-full-suite.sh

# Run just the GPU-focused tests
GPU_SUITE=true ./run-full-suite.sh tests/test_gpu_kai.py

# Explicit environment overrides
WITH_METRICS_SERVER=true \
WITH_KEDA=true \
WITH_VPA=true \
./run-full-suite.sh

# Keep the cluster for inspection
KEEP_KIND_CLUSTER=1 ./run-full-suite.sh

# Run the full suite against Kubernetes v1.35.0 (full stack) and
# v1.32.0 (metrics-server only)
./run-full-matrix-local.sh

# The local suite uses an in-cluster mock Kubex upstream by default.
# Disable it if you want the older local recommendations file flow.
DEPLOY_KUBEX_STUB=false ./run-full-matrix-local.sh

# Run a subset of tests through the matrix bootstrap
./run-full-matrix-local.sh tests/test_automation_strategy.py
./run-full-matrix-local.sh tests/test_policies.py::TestProactivePolicy::test_create_proactive_policy
./run-full-matrix-local.sh tests/test_automation_strategy.py tests/test_policies.py

# Keep the cluster alive between subset reruns while debugging
KEEP_KIND_CLUSTER=1 ./run-full-matrix-local.sh tests/test_policies.py::TestProactivePolicy::test_create_proactive_policy


# Pin a single run to a specific Kind node image
NODE_IMAGE=kindest/node:v1.35.0 \
./run-full-suite.sh

# Override the controller image instead of using chart defaults
CONTROLLER_IMAGE_REPOSITORY=<your-image-repo> \
CONTROLLER_IMAGE_TAG=<your-image-tag> \
./run-full-suite.sh

# Validate the vendored example bundles against the bootstrapped cluster
./run-full-matrix-local.sh tests/test_examples.py

# Exercise the valid examples against a live cluster and assert workload health
./run-full-matrix-local.sh tests/test_example_behavior.py

# Run a single test module
./run-full-matrix-local.sh tests/test_automation_strategy.py

# Run a single test class
./run-full-matrix-local.sh tests/test_policies.py::TestStaticPolicy
```

### CLI Options

| Option | Default | Description |
|---|---|---|
| `--kube-context` | `kind-e2e` | kubectl context to target |
| `--kind-cluster-name` | derived from context | Kind cluster name to create/delete |
| `--kind-node-image` | `kindest/node:v1.35.0` | Kind node image used when creating the cluster |
| `--namespace` | `kubex` | Namespace where the controller is deployed |
| `--test-namespace` | `e2e-test` | Namespace for test workloads (created/deleted per session) |
| `--recommendations-file` | _(none)_ | Path to a JSON recommendations fixture to load |
| `--controller-image-repository` | chart default | Controller image repository override for Helm installation |
| `--controller-image-tag` | chart default | Controller image tag override for Helm installation |
| `--controller-image-pull-policy` | `IfNotPresent` | Controller image pull policy override for Helm installation |
| `--keep-kind-cluster` | `false` | Keep the cluster after the test session |
| `--skip-kind-bootstrap` | `false` | Use the current kube context without creating a cluster |
| `--without-vpa` | `false` | Skip VPA installation |
| `PYTEST_WORKERS` | unset | Optional `pytest-xdist` worker count; leave unset for the default serial run |
| `--without-keda` | `false` | Skip KEDA installation |
| `--without-metrics-server` | `false` | Skip metrics-server installation |

### `run-full-suite.sh` environment variables

| Variable | Default | Description |
|---|---|---|
| `CLUSTER_NAME` | `e2e` | Kind cluster name |
| `NODE_IMAGE` | `kindest/node:v1.35.0` | Kind node image (override to test another version) |
| `KEEP_KIND_CLUSTER` | unset | Set to `true` to pass `--keep-kind-cluster` to pytest and skip the uninstall/teardown step |
| `WITH_METRICS_SERVER` | `true` | Set to `false` to skip metrics-server installation |
| `WITH_KEDA` | `true` | Set to `false` to skip KEDA installation |
| `WITH_VPA` | `true` | Set to `false` to skip VPA installation |
| `HELM_CRDS_CHART` | `kubex/kubex-crds` | Override the kubex-crds chart reference |
| `HELM_CONTROLLER_CHART` | `kubex/kubex-automation-engine` | Override the controller chart reference |
| `HELM_CRDS_CHART_VERSION` | unset | Override the kubex-crds chart version |
| `HELM_CONTROLLER_CHART_VERSION` | unset | Override the controller chart version |
| `HELM_REPO_URL` | chart default | Override the Helm chart repository URL |
| `CONTROLLER_IMAGE_REPOSITORY` | chart default | Controller image repository override |
| `CONTROLLER_IMAGE_TAG` | chart default | Controller image tag override |
| `PYTEST_WORKERS` | unset | Optional `pytest-xdist` worker count |
| `DEPLOY_KUBEX_STUB` | `true` | Deploy an in-cluster Kubex upstream server and point the gateway sidecar at it |
| `KUBEX_URL_HOST` | unset | Override the upstream host used by the gateway sidecar when not using the in-cluster stub |
| `KUBEX_URL_SCHEME` | unset | Override the upstream scheme used by the gateway sidecar when not using the in-cluster stub |
| `GPU_SUITE` | `false` | Enable the GPU feature bootstrap path |
| `GPU_KIND_CONFIG` | unset | Kind config used for the GPU suite |

### Live Validation Notes

- Use `--skip-kind-bootstrap` with an existing cluster when validating a single new or changed test.
- Prefer nodeids or `-k` filters for point fixes instead of booting the full suite.
- GPU live validation requires a real GPU-capable cluster; Kind-based GPU tests still use the GPU bootstrap path.
- The VPA-guarded GPU live test skips automatically when the cluster does not have the `verticalpodautoscalers.autoscaling.k8s.io` CRD.

```bash
# Use an existing cluster without bootstrapping a new Kind environment
pytest tests/ -v \
  --skip-kind-bootstrap \
  --kube-context kind-e2e

# Run a specific e2e test or file against an existing cluster
pytest tests/test_gpu_kai.py::TestGpuLiveValidation::test_gpu_resize_caps_at_previous_whole_gpu -v \
  --skip-kind-bootstrap \
  --kube-context gke_pm-testing-160714_europe-west4-a_kai-demo-cluster

# Run only one focused test module on a live cluster
pytest tests/test_gpu_kai.py -k 'gpu_resize_is_logged_by_the_controller' -v \
  --skip-kind-bootstrap \
  --kube-context gke_pm-testing-160714_europe-west4-a_kai-demo-cluster
```

## Layout

```
e2e-testing/
├── bootstrap.py                     # Kind bootstrap and Helm installation helpers
├── conftest.py                      # CLI options, fixtures, K8sClients dataclass
├── run-full-suite.sh                # Main local entry point
├── run-full-matrix-local.sh         # Root wrapper for the matrix runner
├── examples/                        # Vendored example manifests used by test_examples.py
│   └── invalid/                     # Intentionally invalid examples that should be rejected
├── helpers.py                       # Constants, k8s utilities, manifest builders
├── scripts/
│   └── run-full-matrix-local.sh     # Build local images and run the full Kind version matrix
└── tests/
    ├── test_health.py               # Controller pod, webhooks, metrics smoke tests
    ├── test_crd_validation.py       # Admission webhook schema enforcement
    ├── test_automation_strategy.py  # AutomationStrategy CRUD
    ├── test_policies.py             # StaticPolicy, EnablementGates, ClusterStaticPolicy, ProactivePolicy
    ├── test_global_config.py        # GlobalConfiguration + recommendation reload status
    ├── test_recommendation_behavior.py # Recommendation-content behavior using local fixture data
    ├── test_metrics.py              # Prometheus metrics endpoint
    ├── test_examples.py             # Valid example apply/delete coverage + invalid example rejection
    ├── test_example_behavior.py     # Live-cluster behavior coverage for vendored examples
    ├── test_resize_behavior.py      # Real workload in-place resize vs eviction fallback by Kubernetes version
    ├── test_rollback_behavior.py    # Rollback seed/reseed/cleanup lifecycle on live workloads
    ├── test_webhook.py              # Mutating webhook annotation injection
    ├── test_pod_affinity_policy.py  # StatefulSet PodAffinityPolicy admission mutation
    ├── test_strimzipodset.py        # StrimziPodSet opt-in policy coverage with synthetic owned Pods
    └── test_safety.py              # HPA filter, protected namespace
```

## Test Classes

| Class | Module | Area | Notes |
|---|---|---|---|
| `TestControllerHealth` | `test_health.py` | Pod readiness, webhook certificate, metrics | Smoke tests — run first |
| `TestCRDValidation` | `test_crd_validation.py` | CRD schema enforcement | Verifies required fields, rejects bad specs |
| `TestAutomationStrategy` | `test_automation_strategy.py` | `AutomationStrategy` CRUD | Namespaced; tests all enablement flag combinations |
| `TestStaticPolicy` | `test_policies.py` | `StaticPolicy` CRUD + resource mutation | Creates a Deployment and verifies CPU/mem are updated |
| `TestEnablementGates` | `test_policies.py` | Per-direction enable/disable flags | Verifies downsize-only / upsize-only gate behaviour |
| `TestClusterStaticPolicy` | `test_policies.py` | `ClusterStaticPolicy` namespace selector | `In` applies, `NotIn` excludes — cluster-scoped |
| `TestProactivePolicy` | `test_policies.py` | `ProactivePolicy` CRUD + staleness gate | `maxAnalysisAgeDays=0` edge case |
| `TestGlobalConfiguration` | `test_global_config.py` | `GlobalConfiguration` singleton | Update + revert; verifies persistence via reconciler |
| `TestRecommendations` | `test_global_config.py` | Recommendation load status | Checks `recommendationReload` status fields |
| `TestRecommendationBehavior` | `test_recommendation_behavior.py` | Recommendation-content behavior | Verifies local recommendations mutate matching workloads and respect `KubexAutomation` per container |
| `TestMetrics` | `test_metrics.py` | Prometheus metrics endpoint | Verifies `controller_runtime_reconcile_total` is exposed |
| `TestExampleBehavior` | `test_example_behavior.py` | Live example coverage | Applies every valid vendored example and asserts declared resources exist and workloads become ready |
| `TestHPAExampleBehavior` | `test_example_behavior.py` | Example-backed HPA safety | Applies HPA examples and verifies the controller preserves workload requests |
| `TestResizeBehavior` | `test_resize_behavior.py` | Real workload resize behavior | Verifies pod identity stays stable only when the live cluster actually supports in-place resize, and changes otherwise |
| `TestRollbackBehavior` | `test_rollback_behavior.py` | Rollback monitoring lifecycle | Verifies monitoring seeds, reseeds on new recommendations, and clears rollback annotations after the monitoring window |
| `TestWebhookAnnotations` | `test_webhook.py` | Mutating webhook pod annotation | Checks `automation-webhook.kubex.ai/pod-rightsizing-info`; verifies `PodAdmissionWebhookHealthy` condition |
| `TestPodAffinityPolicy` | `test_pod_affinity_policy.py` | StatefulSet PodAffinityPolicy behavior | Verifies matching StatefulSets get preferred hostname affinity on replacement pods while non-matching StatefulSets stay unchanged |
| `TestHPAFilter` | `test_safety.py` | Safety check: HPA protection | Resize must be blocked when an HPA targets the workload |
| `TestProtectedNamespace` | `test_safety.py` | Safety check: protected namespace patterns | `kube-*` default; custom pattern round-trip |

## Notes

- Kind bootstrap is handled by [bootstrap.py](bootstrap.py).
- The main local entry point is [run-full-suite.sh](run-full-suite.sh).
- [run-full-matrix-local.sh](run-full-matrix-local.sh) builds the local controller images, then runs the full-suite flow twice with GPU enabled in both lanes: once for `v1.35.0` with the full stack (metrics-server, KEDA, VPA) and once for `v1.32.0` with metrics-server only (KEDA and VPA skipped). Pass one or more pytest nodeids/paths to run only that subset through the matrix bootstrap.
- `test_example_behavior.py` now waits for both `Deployment` and `StatefulSet` workloads declared in vendored examples to become ready.
- `test_strimzipodset.py` exercises both `core.strimzi.io/v1` and `core.strimzi.io/v1beta2` using a minimal CRD fixture plus synthetic owned Pods so the controller follows the real owned-pod path.
- The local suite can deploy an in-cluster Python mock Kubex service, feed recommendations from `examples/recommendations.json`, and assert heartbeat/policy/mutation uploads through the real gateway sidecar path.
- The full-suite runner verifies install through the functional tests, then uninstalls the controller Helm release and `kubex-crds` and verifies their removal.
- The bootstrap flow installs `metrics-server`, `KEDA`, and VPA by default. Set `WITH_KEDA=false`, `WITH_VPA=false`, or `WITH_METRICS_SERVER=false` to skip individual addons. The CI matrix uses the full stack plus GPU coverage on v1.35.0 and metrics-server plus GPU coverage on v1.32.0 (`WITH_KEDA=false WITH_VPA=false`).
- The default full-suite runner is serial because many tests mutate shared cluster state and vendored example resources; set `PYTEST_WORKERS` only after isolating those tests.
- Tests can use `supports_in_place_resize` as a coarse version check, but behavior-sensitive tests should gate on the live `actual_in_place_resize_support` probe fixture.
- Test workloads are created in `--test-namespace` and cleaned up after each test class via `autouse` fixtures.
- Recommendation-dependent tests should run with recommendations available, either by passing `--recommendations-file` or by generating recommendation input as part of bootstrap.
- `run-full-suite.sh` defaults `RECOMMENDATIONS_FILE` to `examples/recommendations.json`; set it to another path or to an empty string if you want to disable local recommendation injection.
- The `TestWebhookAnnotations.test_webhook_probe_annotation_handled` test polls `GlobalConfiguration.status.conditions` and may take up to 120 s on a cold cluster.
- `ClusterAutomationStrategy` and `ClusterStaticPolicy` resources created during tests are deleted in teardown; if a test is interrupted run `kubectl delete clusterautomationstrategies,clusterstaticpolicies -l app.kubernetes.io/managed-by=e2e` as a manual cleanup.
