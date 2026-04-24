"""Tests: AutomationStrategy behavior from the mixed-scope example."""

from pathlib import Path

import pytest

from example_utils import (
    apply_manifest,
    delete_manifest_in_reverse,
    skip_reason,
    wait_for_declared_workloads_ready,
)
from helpers import get_pod_resources, pod_is_ready, wait_for


class TestStrategyScopeBehavior:
    """Verify the documented namespaced-and-cluster example mutates as expected."""

    MANIFEST_PATH = (
        Path(__file__).resolve().parents[3]
        / "examples"
        / "staticpolicy"
        / "namespaced-and-cluster.yaml"
    )

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
        def current_pod(namespace: str, deployment: str):
            pods = k8s_clients.core.list_namespaced_pod(
                namespace, label_selector=f"app={deployment}"
            ).items
            live_pods = [p for p in pods if p.metadata.deletion_timestamp is None]
            if not live_pods:
                raise RuntimeError(f"no live pod found for deployment {namespace}/{deployment}")
            return sorted(live_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]

        def default_workload_mutated():
            pod = current_pod("default", "rightsizing-demo")
            resources = get_pod_resources(k8s_clients.core, "default", pod.metadata.name)
            values = resources["demo"]
            return (
                values["requests"].get("cpu") == "200m"
                and values["requests"].get("memory") == "256Mi"
                and values["limits"].get("cpu") == "400m"
                and values["limits"].get("memory") == "512Mi"
            )

        def example_workload_mutated():
            pod = current_pod("example", "rightsizing-demo")
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
            timeout=240,
            message="default namespace workload mutation",
        )
        wait_for(
            example_workload_mutated,
            timeout=240,
            message="example namespace workload mutation",
        )
