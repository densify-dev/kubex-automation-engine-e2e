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
    def test_resize_mechanism_is_classifiable_and_consistent_with_pod_identity(
        self,
        k8s_clients,
    ):
        """Verify a real recommendation-driven resize and classify the observed mode.

        Classification comes from the controller's authoritative mechanism event
        on the original Pod, not from the replacement Pod's admission annotation.
        Eviction execution is recorded on the Pod that was evicted; its successor
        receives an execution-neutral admission summary.
        Once classified, cross-check that pod identity behaved consistently
        with the reported mechanism (in-place keeps the same UID; eviction
        creates a new one).
        """
        def current_pod():
            return get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)

        def pod_resources(pod):
            return get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)

        original_pod = {"value": None}
        resized_pod = {"value": None}
        mechanism = {"value": None}

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

        def resize_mechanism_from_event():
            events = k8s_clients.core.list_namespaced_event(self.NAMESPACE).items
            for event in events:
                involved = event.involved_object
                if involved is None or involved.uid != original_uid:
                    continue
                if event.reason == "PolicyEvaluationInPlaceResize":
                    return "in-place"
                if event.reason == "PolicyEvaluationEvictResize":
                    return "eviction"
            return None

        def resized_pod_ready():
            """Wait for target resources, then classify execution on the original Pod."""
            try:
                pod = current_pod()
            except RuntimeError:
                return False

            resources = pod_resources(pod)
            if (
                pod.metadata.deletion_timestamp is None
                and resources["demo"]["requests"].get("cpu") == "250m"
                and resources["demo"]["requests"].get("memory") == "256Mi"
                and resources["demo"]["limits"].get("cpu") == "400m"
                and resources["demo"]["limits"].get("memory") == "512Mi"
                and pod_is_ready(pod)
            ):
                observed_mechanism = resize_mechanism_from_event()
                if observed_mechanism is None:
                    return False
                resized_pod["value"] = pod
                mechanism["value"] = observed_mechanism
                return True
            return False

        wait_for(
            record_original_pod,
            timeout=120,
            message="initial workload pod readiness",
        )
        original_uid = original_pod["value"].metadata.uid

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

        # Recommendation-driven changes may briefly replace the pod. Poll until
        # a ready pod exists again with the expected recommendation-applied
        # resources and a recorded resize mechanism.
        wait_for(
            resized_pod_ready,
            timeout=480,
            message="resized workload pod with a classified resize mechanism",
        )

        resized_uid = resized_pod["value"].metadata.uid
        if mechanism["value"] == "in-place":
            assert resized_uid == original_uid, (
                "controller reported an in-place resize, but the workload pod's "
                "UID changed"
            )
        else:
            assert resized_uid != original_uid, (
                "controller reported an eviction, but the workload pod kept the "
                "same UID"
            )
