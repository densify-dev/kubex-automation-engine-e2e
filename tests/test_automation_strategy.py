"""Tests: AutomationStrategy CRUD operations."""

import pytest
from kubernetes.client.rest import ApiException

from helpers import GROUP, VERSION, automation_strategy_manifest, get_crd


class TestAutomationStrategy:
    """Create, read, update, delete AutomationStrategy resources."""

    STRATEGY_NAME = "e2e-test-strategy"

    @pytest.fixture(autouse=True)
    def cleanup(self, k8s_clients, test_namespace):
        yield
        try:
            k8s_clients.custom.delete_namespaced_custom_object(
                GROUP, VERSION, test_namespace, "automationstrategies", self.STRATEGY_NAME
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
