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

Controller installation is handled by the Python bootstrap module. It installs the Helm charts using chart defaults by default, and only generates image override values when you pass `--controller-image-repository` and `--controller-image-tag`.

By default the runners also load [examples/recommendations.json](/home/gasare/Kubex/product/current/kubex-automation-engine-e2e/examples/recommendations.json) into a `recommendations` `ConfigMap` and enable the chart's `localRecommendations` mode so recommendation-dependent tests exercise real data instead of just status fields.

VPA is installed by default so the full suite can cover VPA-backed examples and filters. Pass `--without-vpa` when you explicitly want a leaner cluster bootstrap.

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
./scripts/run-full-suite.sh

# Explicit options
./scripts/run-full-suite.sh

# Keep the cluster for inspection
pytest tests/ -v \
  --keep-kind-cluster

# Run the full suite against Kubernetes 1.35 and 1.34 in one command
./scripts/run-kind-version-matrix.sh

# Pin a single run to a specific Kind node image
NODE_IMAGE=kindest/node:v1.35.0 \
./scripts/run-full-suite.sh

# Use an existing cluster without bootstrapping a new Kind environment
pytest tests/ -v \
  --skip-kind-bootstrap \
  --kube-context kind-e2e

# Override the controller image instead of using chart defaults
pytest tests/ -v \
  --controller-image-repository <your-image-repo> \
  --controller-image-tag <your-image-tag>

# Validate the vendored example bundles against the bootstrapped cluster
pytest tests/test_examples.py -v

# Exercise the valid examples against a live cluster and assert workload health
pytest tests/test_example_behavior.py -v

# Run a single test module
pytest tests/test_automation_strategy.py -v

# Run a single test class
pytest tests/test_policies.py::TestStaticPolicy -v

# Run with a timeout (seconds) per test
pytest tests/ -v --timeout=120
```

### CLI Options

| Option | Default | Description |
|---|---|---|
| `--kube-context` | `kind-e2e` | kubectl context to target |
| `--kind-cluster-name` | derived from context | Kind cluster name to create/delete |
| `--kind-node-image` | cluster default | Kind node image used when creating the cluster |
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

## Layout

```
e2e-testing/
├── bootstrap.py                     # Kind bootstrap and Helm installation helpers
├── conftest.py                      # CLI options, fixtures, K8sClients dataclass
├── examples/                        # Vendored example manifests used by test_examples.py
│   └── invalid/                     # Intentionally invalid examples that should be rejected
├── helpers.py                       # Constants, k8s utilities, manifest builders
├── scripts/
│   └── run-full-suite.sh              # Bootstrap, run the functional suite, then verify uninstall
│   └── run-kind-version-matrix.sh     # Wrap run-full-suite.sh for 1.35 and pre-1.35 clusters
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
    ├── test_webhook.py              # Mutating webhook annotation injection
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
| `TestWebhookAnnotations` | `test_webhook.py` | Mutating webhook pod annotation | Checks `automation-webhook.kubex.ai/pod-rightsizing-info`; verifies `PodAdmissionWebhookHealthy` condition |
| `TestHPAFilter` | `test_safety.py` | Safety check: HPA protection | Resize must be blocked when an HPA targets the workload |
| `TestProtectedNamespace` | `test_safety.py` | Safety check: protected namespace patterns | `kube-*` default; custom pattern round-trip |

## Notes

- Kind bootstrap is handled by [bootstrap.py](/home/gasare/Kubex/product/current/kubex-automation-engine-e2e/bootstrap.py).
- The main local entry point is [scripts/run-full-suite.sh](/home/gasare/Kubex/product/current/kubex-automation-engine-e2e/scripts/run-full-suite.sh).
- [scripts/run-kind-version-matrix.sh](/home/gasare/Kubex/product/current/kubex-automation-engine-e2e/scripts/run-kind-version-matrix.sh) runs that same full-suite flow twice, once for `v1.35.0` and once for `v1.34.0`.
- The full-suite runner verifies install through the functional tests, then uninstalls the controller Helm release and `kubex-crds` and verifies their removal.
- Dual-version execution is handled by [scripts/run-kind-version-matrix.sh](/home/gasare/Kubex/product/current/kubex-automation-engine-e2e/scripts/run-kind-version-matrix.sh).
- The bootstrap flow installs `metrics-server`, `KEDA`, and VPA by default; pass `--without-vpa` if you want to skip VPA.
- The default full-suite runner is serial because many tests mutate shared cluster state and vendored example resources; set `PYTEST_WORKERS` only after isolating those tests.
- Tests can use `supports_in_place_resize` as a coarse version check, but behavior-sensitive tests should gate on the live `actual_in_place_resize_support` probe fixture.
- Test workloads are created in `--test-namespace` and cleaned up after each test class via `autouse` fixtures.
- Recommendation-dependent tests should run with recommendations available, either by passing `--recommendations-file` or by generating recommendation input as part of bootstrap.
- `run-full-suite.sh` defaults `RECOMMENDATIONS_FILE` to `examples/recommendations.json`; set it to another path or to an empty string if you want to disable local recommendation injection.
- The `TestWebhookAnnotations.test_webhook_probe_annotation_handled` test polls `GlobalConfiguration.status.conditions` and may take up to 120 s on a cold cluster.
- `ClusterAutomationStrategy` and `ClusterStaticPolicy` resources created during tests are deleted in teardown; if a test is interrupted run `kubectl delete clusterautomationstrategies,clusterstaticpolicies -l app.kubernetes.io/managed-by=e2e` as a manual cleanup.
