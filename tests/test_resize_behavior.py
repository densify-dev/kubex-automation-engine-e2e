"""Tests: real workload resize behavior across Kubernetes versions."""

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_deployment,
    delete_deployment,
    get_deployment_pod,
    get_pod_resources,
    pod_is_ready,
    static_policy_manifest,
    wait_for,
)


class TestResizeBehavior:
    """Verify live workload behavior differs across Kubernetes versions."""

    STRATEGY_NAME = "e2e-resize-strategy"
    POLICY_NAME = "e2e-resize-policy"
    DEPLOYMENT = "e2e-resize-workload"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, test_namespace, plural, name
                )
            except ApiException:
                pass

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "automationstrategies",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "AutomationStrategy",
                "metadata": {"name": self.STRATEGY_NAME, "namespace": test_namespace},
                "spec": {
                    "enablement": {
                        "cpu": {"requests": {"downsize": True, "upsize": True}},
                        "memory": {"requests": {"downsize": True, "upsize": True}},
                    },
                    "safetyChecks": {
                        # Remove the default 10s readiness delay so the test
                        # measures resize behavior rather than controller backoff.
                        "minReadyDuration": "0s",
                    },
                },
            },
        )
        create_deployment(
            k8s_clients.apps,
            test_namespace,
            self.DEPLOYMENT,
            cpu_request="100m",
            mem_request="64Mi",
            cpu_limit="400m",
            mem_limit="256Mi",
        )
        yield
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, test_namespace, plural, name
                )
            except ApiException:
                pass

    @pytest.mark.timeout(900)
    def test_resize_uses_in_place_on_135_and_eviction_before_135(
        self,
        k8s_clients,
        test_namespace,
        supports_in_place_resize,
    ):
        def current_pod():
            return get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)

        def current_resources():
            return get_pod_resources(k8s_clients.core, test_namespace, current_pod().metadata.name)

        resized_pod = {"value": None}

        def resized_pod_ready():
            """Wait for a post-policy pod to exist and carry the expected requests."""
            try:
                pod = current_pod()
            except RuntimeError:
                return False

            resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
            if (
                resources["app"]["requests"].get("cpu") == "250m"
                and resources["app"]["requests"].get("memory") == "192Mi"
                and pod_is_ready(pod)
            ):
                resized_pod["value"] = pod
                return True
            return False

        # Wait for the initial workload pod so we can compare identity before
        # and after the controller applies the StaticPolicy.
        wait_for(
            lambda: pod_is_ready(current_pod()),
            timeout=120,
            message="initial workload pod readiness",
        )
        original_pod = current_pod()
        original_uid = original_pod.metadata.uid

        policy = static_policy_manifest(
            self.POLICY_NAME,
            test_namespace,
            strategy_name=self.STRATEGY_NAME,
            label_selector_app=self.DEPLOYMENT,
            cpu_request="250m",
            mem_request="192Mi",
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "staticpolicies", policy
        )

        # The workload may briefly have no matching pod while the controller
        # replaces it, so keep polling until a matching ready pod exists again
        # with the expected requests.
        wait_for(
            resized_pod_ready,
            timeout=480,
            message="resized workload pod readiness",
        )
        resized_pod_value = resized_pod["value"] or current_pod()

        if supports_in_place_resize:
            assert resized_pod_value.metadata.uid == original_uid, (
                "expected in-place resize on Kubernetes >= 1.35, but the workload pod was replaced"
            )
        else:
            assert resized_pod_value.metadata.uid != original_uid, (
                "expected pod replacement on Kubernetes < 1.35, "
                "but the workload pod UID did not change"
            )
