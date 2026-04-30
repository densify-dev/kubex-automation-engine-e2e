"""Tests: real recommendation contents drive workload changes."""

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
    proactive_policy_manifest,
    wait_for,
)


class TestRecommendationBehavior:
    """Verify real recommendation fixture contents drive live pod changes."""

    STRATEGY_NAME = "e2e-recommendation-strategy"
    POLICY_NAME = "e2e-recommendation-policy"
    CLEANUP_POLICIES = ["e2e-recommendation-policy", "e2e-proactive-policy", "e2e-globalconfig-policy"]
    CLEANUP_STRATEGIES = [
        "e2e-recommendation-strategy",
        "e2e-proactive-strategy",
        "e2e-globalconfig-strategy",
    ]

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients):
        for deployment in ["rightsizing-demo", "mixed-demo", "multi-container-demo"]:
            delete_deployment(k8s_clients.apps, "default", deployment)

        for name in self.CLEANUP_POLICIES:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, "default", "proactivepolicies", name
                )
            except ApiException:
                pass
        for name in self.CLEANUP_STRATEGIES:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, "default", "automationstrategies", name
                )
            except ApiException:
                pass

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            "default",
            "automationstrategies",
            automation_strategy_manifest(self.STRATEGY_NAME, "default"),
        )
        yield
        for deployment in ["rightsizing-demo", "mixed-demo", "multi-container-demo"]:
            delete_deployment(k8s_clients.apps, "default", deployment)
        for name in self.CLEANUP_POLICIES:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, "default", "proactivepolicies", name
                )
            except ApiException:
                pass
        for name in self.CLEANUP_STRATEGIES:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, "default", "automationstrategies", name
                )
            except ApiException:
                pass

    @pytest.mark.timeout(600)
    def test_single_container_recommendation_applied(self, k8s_clients):
        def current_pod():
            return get_deployment_pod(k8s_clients.core, "default", "rightsizing-demo")

        def current_resources():
            return get_pod_resources(k8s_clients.core, "default", current_pod().metadata.name)

        def recommendation_applied():
            pod = current_pod()
            resources = current_resources()
            return (
                pod.metadata.deletion_timestamp is None
                and resources["demo"]["requests"].get("cpu") == "250m"
                and resources["demo"]["requests"].get("memory") == "256Mi"
                and resources["demo"]["limits"].get("cpu") == "400m"
                and resources["demo"]["limits"].get("memory") == "512Mi"
            )

        # Create the policy first so the controller can evaluate the workload as
        # soon as the matching pod appears.
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            "default",
            "proactivepolicies",
            proactive_policy_manifest(self.POLICY_NAME, "default", self.STRATEGY_NAME, 365),
        )
        # The fixture recommendations.json contains a recommendation for
        # default/rightsizing-demo container "demo".
        create_multi_container_deployment(
            k8s_clients.apps,
            "default",
            "rightsizing-demo",
            containers=[
                {
                    "name": "demo",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )

        # Recommendation-driven updates are applied to the live pod. Poll the
        # pod resources directly instead of the Deployment template.
        wait_for(
            recommendation_applied,
            timeout=300,
            message="recommendation-driven resize for rightsizing-demo",
        )

    @pytest.mark.timeout(600)
    def test_kubex_automation_flag_respected_per_container(self, k8s_clients):
        def current_pod():
            return get_deployment_pod(k8s_clients.core, "default", "multi-container-demo")

        def current_resources():
            return get_pod_resources(k8s_clients.core, "default", current_pod().metadata.name)

        def selective_resize_applied():
            pod = current_pod()
            resources = current_resources()
            return (
                pod.metadata.deletion_timestamp is None
                and resources["api"]["requests"].get("cpu") == "200m"
                and resources["api"]["requests"].get("memory") == "256Mi"
                and resources["worker"]["requests"].get("cpu") == "250m"
                and resources["worker"]["requests"].get("memory") == "192Mi"
                and resources["worker"]["limits"].get("cpu") == "450m"
                and resources["worker"]["limits"].get("memory") == "384Mi"
            )

        # The fixture recommendations disable KubexAutomation for the "api"
        # container and enable it for "worker". The test checks that only the
        # worker container is changed.
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            "default",
            "proactivepolicies",
            proactive_policy_manifest(self.POLICY_NAME, "default", self.STRATEGY_NAME, 365),
        )
        create_multi_container_deployment(
            k8s_clients.apps,
            "default",
            "multi-container-demo",
            containers=[
                {
                    "name": "api",
                    "requests": {"cpu": "200m", "memory": "256Mi"},
                    "limits": {"cpu": "400m", "memory": "512Mi"},
                },
                {
                    "name": "worker",
                    "requests": {"cpu": "150m", "memory": "128Mi"},
                    "limits": {"cpu": "300m", "memory": "256Mi"},
                },
            ],
        )

        wait_for(
            selective_resize_applied,
            timeout=300,
            message="per-container KubexAutomation behavior for multi-container-demo",
        )

    @pytest.mark.timeout(600)
    def test_per_container_memory_request_ceiling_clamps_recommendation(self, k8s_clients):
        strategy = k8s_clients.custom.get_namespaced_custom_object(
            GROUP, VERSION, "default", "automationstrategies", self.STRATEGY_NAME
        )
        strategy["spec"]["enablement"].setdefault("memory", {}).setdefault("requests", {})[
            "containers"
        ] = {"api": {"ceiling": "300Mi"}}
        k8s_clients.custom.replace_namespaced_custom_object(
            GROUP,
            VERSION,
            "default",
            "automationstrategies",
            self.STRATEGY_NAME,
            strategy,
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            "default",
            "proactivepolicies",
            proactive_policy_manifest(self.POLICY_NAME, "default", self.STRATEGY_NAME, 365),
        )
        create_multi_container_deployment(
            k8s_clients.apps,
            "default",
            "mixed-demo",
            containers=[
                {
                    "name": "api",
                    "requests": {"cpu": "150m", "memory": "256Mi"},
                    "limits": {"cpu": "300m", "memory": "512Mi"},
                }
            ],
        )

        def ceiling_applied():
            pod = get_deployment_pod(k8s_clients.core, "default", "mixed-demo")
            resources = get_pod_resources(k8s_clients.core, "default", pod.metadata.name)
            return (
                pod.metadata.deletion_timestamp is None
                and resources["api"]["requests"].get("cpu") == "350m"
                and resources["api"]["requests"].get("memory") == "300Mi"
                and resources["api"]["limits"].get("cpu") == "700m"
                and resources["api"]["limits"].get("memory") == "1Gi"
            )

        wait_for(
            ceiling_applied,
            timeout=300,
            message="per-container memory request ceiling for mixed-demo/api",
        )
