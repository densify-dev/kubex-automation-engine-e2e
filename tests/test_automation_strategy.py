"""Tests: AutomationStrategy CRUD operations and scope behavior."""

import pytest
from kubernetes.client.rest import ApiException

from helpers import GROUP, VERSION, automation_strategy_manifest, get_crd


class TestAutomationStrategy:
    """Create, read, update, delete AutomationStrategy resources."""

    STRATEGY_NAME = "e2e-test-strategy"
    CLUSTER_STRATEGY_NAME = "e2e-test-cluster-strategy"
    SHARED_STRATEGY_NAME = "e2e-shared-strategy"

    @pytest.fixture(autouse=True)
    def cleanup(self, k8s_clients, test_namespace):
        yield
        for namespace, name in [
            (test_namespace, self.STRATEGY_NAME),
            (test_namespace, self.SHARED_STRATEGY_NAME),
            (None, self.CLUSTER_STRATEGY_NAME),
            (None, self.SHARED_STRATEGY_NAME),
        ]:
            try:
                if namespace:
                    k8s_clients.custom.delete_namespaced_custom_object(
                        GROUP, VERSION, namespace, "automationstrategies", name
                    )
                else:
                    k8s_clients.custom.delete_cluster_custom_object(
                        GROUP, VERSION, "clusterautomationstrategies", name
                    )
            except ApiException:
                pass

    def test_create_automation_strategy(self, k8s_clients, test_namespace):
        manifest = automation_strategy_manifest(self.STRATEGY_NAME, test_namespace)
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "automationstrategies", manifest
        )
        result = get_crd(
            k8s_clients.custom, "automationstrategies", self.STRATEGY_NAME, test_namespace
        )
        assert result["metadata"]["name"] == self.STRATEGY_NAME

    def test_automation_strategy_defaults_applied(self, k8s_clients, test_namespace):
        manifest = automation_strategy_manifest(self.STRATEGY_NAME, test_namespace)
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "automationstrategies", manifest
        )
        result = get_crd(
            k8s_clients.custom, "automationstrategies", self.STRATEGY_NAME, test_namespace
        )
        cpu_req = result["spec"]["enablement"]["cpu"]["requests"]
        assert "downsize" in cpu_req
        assert "upsize" in cpu_req

    def test_update_automation_strategy(self, k8s_clients, test_namespace):
        manifest = automation_strategy_manifest(self.STRATEGY_NAME, test_namespace)
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "automationstrategies", manifest
        )
        # Fetch current version — resourceVersion is required for updates
        current = get_crd(
            k8s_clients.custom, "automationstrategies", self.STRATEGY_NAME, test_namespace
        )
        current["spec"]["enablement"]["cpu"]["requests"]["downsize"] = False
        k8s_clients.custom.replace_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "automationstrategies", self.STRATEGY_NAME, current
        )
        updated = get_crd(
            k8s_clients.custom, "automationstrategies", self.STRATEGY_NAME, test_namespace
        )
        assert updated["spec"]["enablement"]["cpu"]["requests"]["downsize"] is False

    def test_create_cluster_automation_strategy(self, k8s_clients):
        manifest = automation_strategy_manifest(self.CLUSTER_STRATEGY_NAME)
        k8s_clients.custom.create_cluster_custom_object(
            GROUP, VERSION, "clusterautomationstrategies", manifest
        )
        result = get_crd(
            k8s_clients.custom, "clusterautomationstrategies", self.CLUSTER_STRATEGY_NAME
        )
        assert result["metadata"]["name"] == self.CLUSTER_STRATEGY_NAME
        assert result["kind"] == "ClusterAutomationStrategy"

    def test_cluster_automation_strategy_defaults_applied(self, k8s_clients):
        manifest = automation_strategy_manifest(self.CLUSTER_STRATEGY_NAME)
        k8s_clients.custom.create_cluster_custom_object(
            GROUP, VERSION, "clusterautomationstrategies", manifest
        )
        result = get_crd(
            k8s_clients.custom, "clusterautomationstrategies", self.CLUSTER_STRATEGY_NAME
        )
        cpu_req = result["spec"]["enablement"]["cpu"]["requests"]
        assert cpu_req["downsize"] is True
        assert cpu_req["upsize"] is True

    def test_update_cluster_automation_strategy(self, k8s_clients):
        manifest = automation_strategy_manifest(self.CLUSTER_STRATEGY_NAME)
        k8s_clients.custom.create_cluster_custom_object(
            GROUP, VERSION, "clusterautomationstrategies", manifest
        )
        current = get_crd(
            k8s_clients.custom, "clusterautomationstrategies", self.CLUSTER_STRATEGY_NAME
        )
        current["spec"]["safetyChecks"]["minCpuChangePercent"] = 25
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "clusterautomationstrategies", self.CLUSTER_STRATEGY_NAME, current
        )
        updated = get_crd(
            k8s_clients.custom, "clusterautomationstrategies", self.CLUSTER_STRATEGY_NAME
        )
        assert updated["spec"]["safetyChecks"]["minCpuChangePercent"] == 25

    def test_namespaced_and_cluster_strategies_can_share_name(self, k8s_clients, test_namespace):
        manifest = automation_strategy_manifest(self.SHARED_STRATEGY_NAME, test_namespace)
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "automationstrategies", manifest
        )
        cluster_manifest = automation_strategy_manifest(self.SHARED_STRATEGY_NAME)
        k8s_clients.custom.create_cluster_custom_object(
            GROUP, VERSION, "clusterautomationstrategies", cluster_manifest
        )

        namespaced = get_crd(
            k8s_clients.custom, "automationstrategies", self.SHARED_STRATEGY_NAME, test_namespace
        )
        cluster = get_crd(
            k8s_clients.custom, "clusterautomationstrategies", self.SHARED_STRATEGY_NAME
        )

        assert namespaced["kind"] == "AutomationStrategy"
        assert namespaced["metadata"]["namespace"] == test_namespace
        assert cluster["kind"] == "ClusterAutomationStrategy"
        assert "namespace" not in cluster["metadata"]
