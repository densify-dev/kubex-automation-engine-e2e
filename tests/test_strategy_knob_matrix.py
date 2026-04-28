"""Tests: broader AutomationStrategy knob matrix."""

from datetime import datetime
import time

import pytest
import yaml

from example_utils import EXAMPLES_ROOT, apply_manifest, delete_manifest_in_reverse, manifest_documents, skip_reason
from helpers import apply_manifest as apply_manifest_body
from helpers import get_crd, get_pod_resources, pod_is_ready, wait_for


class TestStrategyKnobMatrix:
    """Verify common strategy knobs keep or change workloads as intended."""

    @staticmethod
    def _creation_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

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
                        {"app": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}},
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
                        {"app": {"cpu": "500m", "memory": "512Mi", "limits_cpu": "1", "limits_memory": "1Gi"}},
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
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
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

        try:
            apply_manifest(manifest_path, kube_context)

            if manifest_path == EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster-same-weight.yaml":
                def static_policy(namespace: str, name: str):
                    plural = "staticpolicies" if namespace else "clusterstaticpolicies"
                    return get_crd(k8s_clients.custom, plural, name, namespace)

                wait_for(
                    lambda: bool(
                        static_policy("default", "sample-rightsizing-policy")
                        and static_policy(None, "sample-cluster-scoped-rightsizing-policy")
                    ),
                    timeout=180,
                    message="same-weight policies to exist",
                )

                cluster_doc = next(
                    doc
                    for doc in manifest_documents(manifest_path)
                    if doc["kind"] == "ClusterStaticPolicy"
                )
                k8s_clients.custom.delete_cluster_custom_object(
                    "rightsizing.kubex.ai",
                    "v1alpha1",
                    "clusterstaticpolicies",
                    "sample-cluster-scoped-rightsizing-policy",
                )
                time.sleep(2)
                apply_manifest_body(yaml.safe_dump(cluster_doc, sort_keys=False), kube_context)

                namespaced_policy = static_policy("default", "sample-rightsizing-policy")
                cluster_policy = static_policy(None, "sample-cluster-scoped-rightsizing-policy")
                assert self._creation_timestamp(
                    namespaced_policy["metadata"]["creationTimestamp"]
                ) < self._creation_timestamp(cluster_policy["metadata"]["creationTimestamp"])

            def current_pod(namespace: str, deployment: str):
                pods = k8s_clients.core.list_namespaced_pod(
                    namespace, label_selector=f"app={deployment}"
                ).items
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
                pod = current_pod(namespace, deployment)
                resources = get_pod_resources(k8s_clients.core, namespace, pod.metadata.name)
                for container, values in expected.items():
                    container_resources = resources[container]
                    assert container_resources["requests"].get("cpu") == values["cpu"]
                    assert container_resources["requests"].get("memory") == values["memory"]
                    assert container_resources["limits"].get("cpu") == values["limits_cpu"]
                    assert container_resources["limits"].get("memory") == values["limits_memory"]
        finally:
            delete_manifest_in_reverse(manifest_path, kube_context)
