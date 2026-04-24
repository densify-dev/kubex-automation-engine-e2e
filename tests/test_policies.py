"""Tests: ProactivePolicy CRUD and staleness safety check."""

import time

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    automation_strategy_manifest,
    create_multi_container_deployment,
    delete_deployment,
    get_crd,
    get_deployment_pod,
    get_pod_resources,
    proactive_policy_manifest,
)


class TestProactivePolicy:
    """Verify ProactivePolicy is accepted and reflects safetyChecks configuration."""

    STRATEGY_NAME = "e2e-proactive-strategy"
    POLICY_NAME = "e2e-proactive-policy"
    DEPLOYMENT = "rightsizing-demo"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        # Pre-cleanup
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        for plural, name in [
            ("proactivepolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, test_namespace, plural, name
                )
                time.sleep(1)
            except ApiException:
                pass
        delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "automationstrategies",
            automation_strategy_manifest(self.STRATEGY_NAME, test_namespace),
        )
        yield
        delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)
        for plural, name in [
            ("proactivepolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, test_namespace, plural, name
                )
            except ApiException:
                pass

    def test_create_proactive_policy(self, k8s_clients, test_namespace):
        manifest = proactive_policy_manifest(
            self.POLICY_NAME,
            test_namespace,
            strategy_name=self.STRATEGY_NAME,
            max_analysis_age_days=7,
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "proactivepolicies", manifest
        )
        result = get_crd(k8s_clients.custom, "proactivepolicies", self.POLICY_NAME, test_namespace)
        assert result["spec"]["safetyChecks"]["maxAnalysisAgeDays"] == 7

    @pytest.mark.timeout(600)
    def test_proactive_policy_stale_recommendations_not_applied(self, k8s_clients, test_namespace):
        """A ProactivePolicy should leave workload resources unchanged when recommendations are stale."""
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "proactivepolicies",
            proactive_policy_manifest(self.POLICY_NAME, test_namespace, self.STRATEGY_NAME, 1),
        )
        create_multi_container_deployment(
            k8s_clients.apps,
            test_namespace,
            self.DEPLOYMENT,
            containers=[
                {
                    "name": "demo",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )

        time.sleep(20)
        pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
        resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
        assert resources["demo"]["requests"].get("cpu") == "100m"
        assert resources["demo"]["requests"].get("memory") == "128Mi"
        assert resources["demo"]["limits"].get("cpu") == "200m"
        assert resources["demo"]["limits"].get("memory") == "256Mi"
