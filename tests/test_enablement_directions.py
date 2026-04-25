"""Tests: AutomationStrategy enablement directions for requests and limits."""

import pytest

from example_utils import EXAMPLES_ROOT, apply_manifest, delete_manifest_in_reverse, skip_reason
from helpers import get_pod_resources, pod_is_ready, wait_for


class TestEnablementDirections:
    """Verify strategy direction knobs block and allow the right mutations."""

    MANIFEST_PATH = EXAMPLES_ROOT / "staticpolicy" / "enablement-directions.yaml"
    NAMESPACE = "default"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, kube_context):
        reason = skip_reason(self.MANIFEST_PATH, kube_context)
        if reason:
            pytest.skip(reason)
        apply_manifest(self.MANIFEST_PATH, kube_context)
        yield
        delete_manifest_in_reverse(self.MANIFEST_PATH, kube_context)

    @pytest.mark.timeout(600)
    def test_enablement_directions_respect_allowed_and_blocked_changes(self, k8s_clients):
        def current_pod(name: str):
            pods = k8s_clients.core.list_namespaced_pod(
                self.NAMESPACE, label_selector=f"app={name}"
            ).items
            ready_pods = [p for p in pods if p.metadata.deletion_timestamp is None and pod_is_ready(p)]
            if not ready_pods:
                raise RuntimeError(f"no ready pod found for deployment {name}")
            return sorted(ready_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]

        def ready(name: str) -> bool:
            try:
                return pod_is_ready(current_pod(name))
            except RuntimeError:
                return False

        wait_for(lambda: ready("rightsizing-enablement-direction"), timeout=180, message="enablement-direction workload readiness")

        def allow_directions_applied():
            pod = current_pod("rightsizing-enablement-direction")
            resources = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)
            values = resources["allow-directions"]
            return (
                pod.metadata.deletion_timestamp is None
                and values["requests"].get("cpu") == "500m"
                and values["requests"].get("memory") == "512Mi"
                and values["limits"].get("cpu") == "800m"
                and values["limits"].get("memory") == "1Gi"
            )

        def block_directions_held():
            pod = current_pod("rightsizing-enablement-direction")
            resources = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)
            values = resources["block-directions"]
            return (
                pod.metadata.deletion_timestamp is None
                and values["requests"].get("cpu") == "600m"
                and values["requests"].get("memory") == "256Mi"
                and values["limits"].get("cpu") == "700m"
                and values["limits"].get("memory") == "2Gi"
            )

        def unset_resources_resolved():
            pod = current_pod("rightsizing-enablement-direction")
            resources = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)
            values = resources["unset-resources"]
            return (
                pod.metadata.deletion_timestamp is None
                and values["requests"].get("cpu") == "800m"
                and values["requests"].get("memory") == "512Mi"
                and values["limits"].get("cpu") == "800m"
                and values["limits"].get("memory") is None
            )

        wait_for(allow_directions_applied, timeout=240, message="allowed enablement-direction mutation")
        wait_for(block_directions_held, timeout=240, message="blocked enablement-direction mutation")
        wait_for(unset_resources_resolved, timeout=240, message="setFromUnspecified enablement-direction mutation")
