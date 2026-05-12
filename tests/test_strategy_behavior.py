"""Tests: AutomationStrategy behavior from examples and live policy changes."""

import time

import pytest
from kubernetes.client.rest import ApiException

from example_utils import (
    EXAMPLES_ROOT,
    apply_manifest,
    delete_manifest_in_reverse,
    skip_reason,
    wait_for_declared_workloads_ready,
)
from helpers import (
    GROUP,
    STATIC_POLICY_ANNOTATION,
    VERSION,
    create_deployment,
    delete_deployment,
    get_pod_resources,
    static_policy_manifest,
    wait_for,
)


class TestStrategyScopeBehavior:
    """Verify the documented namespaced-and-cluster example mutates as expected."""

    MANIFEST_PATH = EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster.yaml"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, kube_context):
        reason = skip_reason(self.MANIFEST_PATH, kube_context)
        if reason:
            pytest.skip(reason)
        apply_manifest(self.MANIFEST_PATH, kube_context)
        yield
        delete_manifest_in_reverse(self.MANIFEST_PATH, kube_context)

    @pytest.mark.timeout(600)
    def test_namespaced_and_cluster_strategy_example_applies_expected_resources(
        self, k8s_clients
    ):
        def current_pod(namespace: str, deployment: str, expected=None):
            pods = k8s_clients.core.list_namespaced_pod(
                namespace, label_selector=f"app={deployment}"
            ).items
            live_pods = [p for p in pods if p.metadata.deletion_timestamp is None]
            if not live_pods:
                raise RuntimeError(f"no live pod found for deployment {namespace}/{deployment}")

            if expected is not None:
                matching_pods = []
                for pod in live_pods:
                    resources = get_pod_resources(k8s_clients.core, namespace, pod.metadata.name)
                    if all(
                        resources[container]["requests"].get("cpu") == values["cpu"]
                        and resources[container]["requests"].get("memory") == values["memory"]
                        and resources[container]["limits"].get("cpu") == values["limits_cpu"]
                        and resources[container]["limits"].get("memory") == values["limits_memory"]
                        for container, values in expected.items()
                    ):
                        matching_pods.append(pod)
                if matching_pods:
                    return sorted(matching_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]
                raise RuntimeError(
                    f"no pod found for deployment {namespace}/{deployment} with expected resources"
                )

            return sorted(live_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]

        def default_workload_mutated():
            expected = {
                "demo": {
                    "cpu": "200m",
                    "memory": "296Mi",
                    "limits_cpu": "400m",
                    "limits_memory": "596Mi",
                }
            }
            pod = current_pod("default", "rightsizing-demo", expected)
            resources = get_pod_resources(k8s_clients.core, "default", pod.metadata.name)
            values = resources["demo"]
            return (
                values["requests"].get("cpu") == "200m"
                and values["requests"].get("memory") == "296Mi"
                and values["limits"].get("cpu") == "400m"
                and values["limits"].get("memory") == "596Mi"
            )

        def example_workload_mutated():
            expected = {
                "demo": {
                    "cpu": "200m",
                    "memory": "296Mi",
                    "limits_cpu": "400m",
                    "limits_memory": "596Mi",
                }
            }
            pod = current_pod("example", "rightsizing-demo", expected)
            resources = get_pod_resources(k8s_clients.core, "example", pod.metadata.name)
            values = resources["demo"]
            return (
                values["requests"].get("cpu") == "200m"
                and values["requests"].get("memory") == "296Mi"
                and values["limits"].get("cpu") == "400m"
                and values["limits"].get("memory") == "596Mi"
            )

        wait_for_declared_workloads_ready(self.MANIFEST_PATH, k8s_clients)
        wait_for(
            default_workload_mutated,
            timeout=600,
            message="default namespace workload mutation",
        )
        wait_for(
            example_workload_mutated,
            timeout=600,
            message="example namespace workload mutation",
        )


class TestStrategySchedulingBehavior:
    """Verify scheduling windows block and resume controller-driven resizing."""

    STRATEGY_NAME = "e2e-scheduling-strategy"
    POLICY_NAME = "e2e-scheduling-policy"
    DEPLOYMENT = "e2e-scheduling-workload"
    NAMESPACE = "default"

    def _cleanup(self, k8s_clients):
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, plural, name
                )
            except ApiException:
                pass
        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        try:
            k8s_clients.custom.delete_namespaced_custom_object(
                GROUP, VERSION, self.NAMESPACE, "automationstrategies", self.STRATEGY_NAME
            )
        except ApiException:
            pass

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients):
        self._cleanup(k8s_clients)
        yield
        self._cleanup(k8s_clients)

    @staticmethod
    def _strategy(name: str, namespace: str, blocked: bool) -> dict:
        scheduling = {
            "exclusionWindows": [
                {"name": "always-block", "timezone": "UTC", "start": "00:00", "end": "24:00"}
            ]
        }
        if not blocked:
            scheduling = {
                "inclusionWindows": [
                    {"name": "always-open", "timezone": "UTC", "start": "00:00", "end": "24:00"}
                ]
            }
        return {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "AutomationStrategy",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "inPlaceResize": {"enabled": True},
                "podEviction": {"enabled": True},
                "scheduling": scheduling,
                "safetyChecks": {"minReadyDuration": "0s", "resizeRetryInterval": "5s"},
            },
        }

    @pytest.mark.timeout(900)
    def test_scheduling_exclusion_window_blocks_runtime_resize(self, k8s_clients):
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "automationstrategies",
            self._strategy(self.STRATEGY_NAME, self.NAMESPACE, blocked=True),
        )
        create_deployment(
            k8s_clients.apps,
            self.NAMESPACE,
            self.DEPLOYMENT,
            cpu_request="100m",
            mem_request="64Mi",
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "staticpolicies",
            static_policy_manifest(
                self.POLICY_NAME,
                self.NAMESPACE,
                strategy_name=self.STRATEGY_NAME,
                label_selector_app=self.DEPLOYMENT,
                cpu_request="150m",
                mem_request="96Mi",
            ),
        )

        def latest_live_pod():
            pods = k8s_clients.core.list_namespaced_pod(
                self.NAMESPACE, label_selector=f"app={self.DEPLOYMENT}"
            ).items
            live_pods = [pod for pod in pods if pod.metadata.deletion_timestamp is None]
            if not live_pods:
                raise RuntimeError("no live scheduling test pod found")
            return sorted(live_pods, key=lambda item: item.metadata.creation_timestamp)[-1]

        def latest_live_pod_resources():
            pod = latest_live_pod()
            return get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)

        def deployment_has_static_policy_annotation():
            try:
                dep = k8s_clients.apps.read_namespaced_deployment(self.DEPLOYMENT, self.NAMESPACE)
                return bool(
                    dep.metadata.annotations
                    and STATIC_POLICY_ANNOTATION in dep.metadata.annotations
                )
            except ApiException:
                return False

        # Wait until the StaticPolicy reconciler has written the desired-resource
        # annotation to the Deployment.  This is the correct readiness gate for
        # StaticPolicy workloads: RIGHTSIZING_ANNOTATION
        # ("automation-webhook.kubex.ai/pod-rightsizing-info") is only written by
        # the webhook at pod admission time and will never appear on a pod that was
        # admitted before the policy existed.  The Deployment-level annotation
        # "static.rightsizing.kubex.ai/desired-resource-requests" is written by
        # policy_reconciler as soon as the StaticPolicy is reconciled and global
        # config is ready (including the admission webhook probe).  The 180 s
        # budget covers: controller leader-election (~30 s), global-config reconcile
        # (~10 s requeue), webhook probe completion (~60 s on fresh clusters), and
        # the policy reconcile itself.
        wait_for(
            deployment_has_static_policy_annotation,
            timeout=180,
            message="static policy annotation on deployment",
        )

        initial_pod = latest_live_pod()
        initial_pod_name = initial_pod.metadata.name

        time.sleep(20)
        resources = get_pod_resources(k8s_clients.core, self.NAMESPACE, initial_pod_name)["app"]
        assert (
            resources["requests"].get("cpu") == "100m"
            and resources["requests"].get("memory") == "64Mi"
        ), "resize should remain blocked by the active exclusion window"

        # ── Phase 2: open the scheduling window and verify the deferred resize lands ──
        # Switch the strategy from an always-blocked exclusion window to an
        # always-open inclusion window.  The controller watches AutomationStrategy
        # changes and re-enqueues affected pods; with resizeRetryInterval=5s the
        # resize should arrive well within the budget below.
        for attempt in range(5):
            try:
                existing = k8s_clients.custom.get_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, "automationstrategies", self.STRATEGY_NAME
                )
                open_strategy = self._strategy(self.STRATEGY_NAME, self.NAMESPACE, blocked=False)
                open_strategy["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
                k8s_clients.custom.replace_namespaced_custom_object(
                    GROUP,
                    VERSION,
                    self.NAMESPACE,
                    "automationstrategies",
                    self.STRATEGY_NAME,
                    open_strategy,
                )
                break
            except ApiException as exc:
                if exc.status != 409 or attempt == 4:
                    raise
                time.sleep(1)

        def resources_resized():
            resources = latest_live_pod_resources()["app"]
            return (
                resources["requests"].get("cpu") == "150m"
                and resources["requests"].get("memory") == "96Mi"
            )

        wait_for(
            resources_resized,
            timeout=450,
            message="deferred resize applied after scheduling window opens",
        )
