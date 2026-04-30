"""Tests: broader AutomationStrategy knob matrix."""

import time

import pytest

from example_utils import (
    EXAMPLES_ROOT,
    apply_manifest,
    delete_manifest_in_reverse,
    manifest_documents,
    skip_reason,
)
from helpers import apply_manifest as apply_manifest_object
from helpers import get_pod_resources, pod_is_ready, wait_for


class TestStrategyKnobMatrix:
    """Verify common strategy knobs keep or change workloads as intended."""

    @pytest.mark.parametrize(
        ("manifest_path", "assertions", "sleep_seconds"),
        [
            (
                EXAMPLES_ROOT / "automationstrategy" / "vpa-filter-default.yaml",
                [
                    ("automationstrategy-vpa-default", "vpa-demo", {"app": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}}),
                ],
                45,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "limit-range-filter.yaml",
                [
                    (
                        "automationstrategy-limitrange",
                        "limitrange-demo",
                        {"app": {"cpu": "300m", "memory": "384Mi", "limits_cpu": "600m", "limits_memory": "768Mi"}},
                    ),
                ],
                20,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "pod-limit-range-filter.yaml",
                [
                    (
                        "automationstrategy-podlimitrange",
                        "pod-limitrange-demo",
                        {
                            "app-a": {
                                "cpu": "300m",
                                "memory": "256Mi",
                                "limits_cpu": "500m",
                                "limits_memory": "512Mi",
                            },
                            "app-b": {
                                "cpu": "300m",
                                "memory": "256Mi",
                                "limits_cpu": "500m",
                                "limits_memory": "512Mi",
                            },
                        },
                    ),
                ],
                20,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "min-change-thresholds.yaml",
                [
                    (
                        "automationstrategy-minchange",
                        "min-change-demo",
                        {"app": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}},
                    ),
                ],
                20,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "min-ready-seconds.yaml",
                [
                    (
                        "automationstrategy-ready",
                        "min-ready-demo",
                        {"app": {"cpu": "300m", "memory": "384Mi", "limits_cpu": "800m", "limits_memory": "768Mi"}},
                    ),
                ],
                20,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "node-allocatable-headroom.yaml",
                [
                    (
                        "automationstrategy-node",
                        "node-allocatable-demo",
                        {"app": {"memory": "16Gi", "limits_cpu": "12", "limits_memory": "24Gi"}},
                    ),
                ],
                20,
            ),
            (
                EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster.yaml",
                [
                    (
                        "default",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}},
                    ),
                    (
                        "example",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                ],
                20,
            ),
            (
                EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster-namespace-wins.yaml",
                [
                    (
                        "default",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}},
                    ),
                    (
                        "example",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                ],
                20,
            ),
            (
                EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster-same-weight.yaml",
                [
                    (
                        "default",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}},
                    ),
                    (
                        "example",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                ],
                20,
            ),
        ],
        ids=[
            "automationstrategy/vpa-filter-default",
            "automationstrategy/limit-range-filter",
            "automationstrategy/pod-limit-range-filter",
            "automationstrategy/min-change-thresholds",
            "automationstrategy/min-ready-seconds",
            "automationstrategy/node-allocatable-headroom",
            "staticpolicy/namespaced-and-cluster",
            "staticpolicy/namespaced-and-cluster-namespace-wins",
            "staticpolicy/namespaced-and-cluster-same-weight",
        ],
    )
    @pytest.mark.timeout(1800)
    def test_strategy_knobs_keep_expected_behavior(
        self,
        manifest_path,
        assertions,
        sleep_seconds,
        kube_context,
        k8s_clients,
    ):
        reason = skip_reason(manifest_path, kube_context)
        if reason:
            pytest.skip(reason)

        def apply_ordered_manifest(path):
            if path != EXAMPLES_ROOT / "automationstrategy" / "min-ready-seconds.yaml":
                apply_manifest(path, kube_context)
                return

            docs = manifest_documents(path)
            deployment_docs = [doc for doc in docs if doc["kind"] == "Deployment"]
            prerequisite_docs = [doc for doc in docs if doc["kind"] != "Deployment"]

            for doc in prerequisite_docs:
                apply_manifest_object(doc, kube_context)

            wait_for(
                lambda: k8s_clients.custom.get_namespaced_custom_object(
                    "rightsizing.kubex.ai",
                    "v1alpha1",
                    "automationstrategy-ready",
                    "automationstrategies",
                    "min-ready-strategy",
                ),
                timeout=60,
                message="min-ready strategy availability",
            )
            wait_for(
                lambda: k8s_clients.custom.get_namespaced_custom_object(
                    "rightsizing.kubex.ai",
                    "v1alpha1",
                    "automationstrategy-ready",
                    "staticpolicies",
                    "min-ready-policy",
                ),
                timeout=60,
                message="min-ready policy availability",
            )

            for doc in deployment_docs:
                apply_manifest_object(doc, kube_context)

        try:
            apply_ordered_manifest(manifest_path)

            def current_pod(namespace: str, deployment: str, expected=None):
                pods = k8s_clients.core.list_namespaced_pod(
                    namespace, label_selector=f"app={deployment}"
                ).items
                if expected is not None:
                    matching_pods = []
                    for pod in pods:
                        if pod.metadata.deletion_timestamp is not None:
                            continue
                        resources = get_pod_resources(k8s_clients.core, namespace, pod.metadata.name)
                        if all(
                            (
                                values.get("cpu") is None
                                or resources[container]["requests"].get("cpu") == values["cpu"]
                            )
                            and (
                                values.get("memory") is None
                                or resources[container]["requests"].get("memory") == values["memory"]
                            )
                            and (
                                values.get("limits_cpu") is None
                                or resources[container]["limits"].get("cpu") == values["limits_cpu"]
                            )
                            and (
                                values.get("limits_memory") is None
                                or resources[container]["limits"].get("memory") == values["limits_memory"]
                            )
                            for container, values in expected.items()
                        ):
                            matching_pods.append(pod)
                    if matching_pods:
                        return sorted(matching_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]
                    raise RuntimeError(
                        f"no pod found for deployment {namespace}/{deployment} with expected resources"
                    )

                ready_pods = [
                    p for p in pods if p.metadata.deletion_timestamp is None and pod_is_ready(p)
                ]
                if not ready_pods:
                    raise RuntimeError(f"no ready pod found for deployment {namespace}/{deployment}")

                return sorted(ready_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]

            time.sleep(5)
            for namespace, deployment, _ in assertions:
                wait_for(
                    lambda ns=namespace, dep=deployment: pod_is_ready(current_pod(ns, dep)),
                    timeout=180,
                    message=f"workload readiness for {namespace}/{deployment}",
                )

            time.sleep(sleep_seconds)

            for namespace, deployment, expected in assertions:
                wait_for(
                    lambda ns=namespace, dep=deployment, exp=expected: current_pod(ns, dep, exp),
                    timeout=600,
                    message=f"expected resources for {namespace}/{deployment}",
                )
                pod = current_pod(namespace, deployment, expected)
                resources = get_pod_resources(k8s_clients.core, namespace, pod.metadata.name)
                for container, values in expected.items():
                    container_resources = resources[container]
                    if values.get("cpu") is not None:
                        assert container_resources["requests"].get("cpu") == values["cpu"]
                    if values.get("memory") is not None:
                        assert container_resources["requests"].get("memory") == values["memory"]
                    if values.get("limits_cpu") is not None:
                        assert container_resources["limits"].get("cpu") == values["limits_cpu"]
                    if values.get("limits_memory") is not None:
                        assert container_resources["limits"].get("memory") == values["limits_memory"]
        finally:
            delete_manifest_in_reverse(manifest_path, kube_context)
