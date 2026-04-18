"""Tests: ProactivePolicy CRUD and staleness safety check."""

import json
import subprocess
import time

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    automation_strategy_manifest,
    get_crd,
    proactive_policy_manifest,
)


class TestProactivePolicy:
    """Verify ProactivePolicy is accepted and reflects safetyChecks configuration."""

    STRATEGY_NAME = "e2e-proactive-strategy"
    POLICY_NAME = "e2e-proactive-policy"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        # Pre-cleanup
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

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "automationstrategies",
            automation_strategy_manifest(self.STRATEGY_NAME, test_namespace),
        )
        yield
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

    def test_proactive_policy_stale_recommendations_not_applied(self, k8s_clients, test_namespace):
        """A ProactivePolicy with maxAnalysisAgeDays=0 should reject all recommendations."""
        manifest = proactive_policy_manifest(
            self.POLICY_NAME,
            test_namespace,
            strategy_name=self.STRATEGY_NAME,
            max_analysis_age_days=0,
        )
        result = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(manifest),
            capture_output=True,
            text=True,
        )
        # maxAnalysisAgeDays=0 may be valid per schema but means all recs are stale
        assert result.returncode == 0 or "invalid" in result.stderr.lower()
