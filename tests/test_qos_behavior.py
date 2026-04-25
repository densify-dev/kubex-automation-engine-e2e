"""Tests: controller keeps requests aligned to limits (Guaranteed QoS) on resize."""

import pytest

from example_utils import EXAMPLES_ROOT, apply_manifest, delete_manifest_in_reverse
from helpers import get_deployment_pod, get_pod_resources, pod_is_ready, wait_for


class TestQoSBehavior:
    """Verify the retain-guaranteed-qos example keeps requests aligned to limits after mutation."""

    MANIFEST_PATH = EXAMPLES_ROOT / "staticpolicy" / "retain-guaranteed-qos.yaml"
    NAMESPACE = "default"
    DEPLOYMENT = "retain-guaranteed-demo"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, kube_context):
        apply_manifest(self.MANIFEST_PATH, kube_context)
        yield
        delete_manifest_in_reverse(self.MANIFEST_PATH, kube_context)

    @pytest.mark.timeout(900)
    def test_requests_equal_limits_after_static_policy(self, k8s_clients):
        """After the controller applies a StaticPolicy, requests must stay aligned to limits."""

        def current_pod():
            return get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)

        resized_pod = {"value": None}

        def guaranteed_pod_ready():
            """Wait for a live, ready pod where the controller changed resources and kept Guaranteed QoS."""
            try:
                pod = current_pod()
            except RuntimeError:
                return False

            resources = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)
            app = resources.get("app", {})
            requests = app.get("requests", {})
            limits = app.get("limits", {})

            if (
                pod.metadata.deletion_timestamp is None
                and requests.get("cpu") == limits.get("cpu")
                and requests.get("memory") == limits.get("memory")
                and (
                    requests.get("cpu") != "400m"
                    or requests.get("memory") != "256Mi"
                )
                and pod_is_ready(pod)
            ):
                resized_pod["value"] = pod
                return True
            return False

        wait_for(
            guaranteed_pod_ready,
            timeout=480,
            message="Guaranteed QoS pod after StaticPolicy mutation (requests == limits)",
        )

        pod = resized_pod["value"]
        assert pod.status.qos_class == "Guaranteed", (
            f"expected pod QoS class to be Guaranteed after requests==limits resize, "
            f"got {pod.status.qos_class}"
        )
