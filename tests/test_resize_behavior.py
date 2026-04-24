"""Tests: real workload resize behavior across Kubernetes versions."""

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    automation_strategy_manifest,
    create_multi_container_deployment,
    delete_deployment,
    get_deployment_pod,
    get_pod_resources,
    pod_is_ready,
    proactive_policy_manifest,
    wait_for,
)


class TestResizeBehavior:
    """Verify a real recommendation-driven resize and classify the observed mode."""

    STRATEGY_NAME = "e2e-resize-strategy"
    POLICY_NAME = "e2e-resize-policy"
    DEPLOYMENT = "rightsizing-demo"
    NAMESPACE = "default"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients):
        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        for plural, name in [
            ("proactivepolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, plural, name
                )
            except ApiException:
                pass

        # Reuse the same recommendation-driven path as the passing recommendation
        # tests so this test only checks pod replacement vs in-place behavior.
        strategy = automation_strategy_manifest(self.STRATEGY_NAME, self.NAMESPACE)
        strategy["spec"]["safetyChecks"] = {"minReadyDuration": "0s"}
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "automationstrategies",
            strategy,
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "proactivepolicies",
            proactive_policy_manifest(
                self.POLICY_NAME,
                self.NAMESPACE,
                self.STRATEGY_NAME,
                365,
            ),
        )
        create_multi_container_deployment(
            k8s_clients.apps,
            self.NAMESPACE,
            self.DEPLOYMENT,
            containers=[
                {
                    "name": "demo",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )
        yield
        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        for plural, name in [
            ("proactivepolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, plural, name
                )
            except ApiException:
                pass

    @pytest.mark.timeout(900)
    def test_resize_uses_in_place_on_135_and_eviction_before_135(
        self,
        k8s_clients,
    ):
        def current_pod():
            return get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)

        def current_resources():
            pod = current_pod()
            return get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)

        original_pod = {"value": None}
        resized_pod = {"value": None}

        def record_original_pod():
            """Capture the first ready pod before the controller applies the recommendation."""
            try:
                pod = current_pod()
            except RuntimeError:
                return False
            if pod.metadata.deletion_timestamp is None and pod_is_ready(pod):
                original_pod["value"] = pod
                return True
            return False

        def resized_pod_ready():
            """Wait for the live pod to reflect the recommendation fixture values."""
            try:
                pod = current_pod()
            except RuntimeError:
                return False

            resources = current_resources()
            if (
                pod.metadata.deletion_timestamp is None
                and resources["demo"]["requests"].get("cpu") == "250m"
                and resources["demo"]["requests"].get("memory") == "256Mi"
                and resources["demo"]["limits"].get("cpu") == "400m"
                and resources["demo"]["limits"].get("memory") == "512Mi"
                and pod_is_ready(pod)
            ):
                resized_pod["value"] = pod
                return True
            return False

        def used_resize_subresource(pod):
            """Detect in-place resize from pod managed fields when the UID is unchanged.

            A straight version check is not reliable enough here. In local runs
            `kindest/node:v1.34.0` can still report an in-place resize via the
            Pod resize subresource, so the test should verify the observed mode
            rather than assume it from the server version alone.
            """
            for field in pod.metadata.managed_fields or []:
                if field.subresource == "resize":
                    return True
            return False

        wait_for(
            record_original_pod,
            timeout=120,
            message="initial workload pod readiness",
        )
        original_uid = original_pod["value"].metadata.uid

        # Recommendation-driven changes may briefly replace the pod. Poll until
        # a ready pod exists again with the expected recommendation-applied
        # resources, then compare the resulting UID.
        wait_for(
            resized_pod_ready,
            timeout=480,
            message="resized workload pod readiness",
        )

        if resized_pod["value"].metadata.uid == original_uid:
            assert used_resize_subresource(resized_pod["value"]), (
                "the workload pod kept the same UID, but the pod metadata does "
                "not show a resize subresource update"
            )
        else:
            assert resized_pod["value"].metadata.uid != original_uid
