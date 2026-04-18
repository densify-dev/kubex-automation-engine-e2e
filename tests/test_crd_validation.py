"""Tests: admission webhook validation rejects invalid CRD specs."""

import json
import subprocess

from helpers import GROUP, VERSION


class TestCRDValidation:
    """Verify admission webhook validation rejects invalid CRD specs."""

    def test_automation_strategy_invalid_bounds_rejected(self, kube_context, test_namespace):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "AutomationStrategy",
            "metadata": {"name": "bad-bounds", "namespace": test_namespace},
            "spec": {
                "enablement": {
                    "cpu": {"requests": {"floor": "1000m", "ceiling": "100m"}}  # floor > ceiling
                }
            },
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected validation rejection for floor > ceiling"
        assert "floor" in result.stderr.lower() or "ceiling" in result.stderr.lower()

    def test_static_policy_missing_strategy_ref_rejected(self, kube_context, test_namespace):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "StaticPolicy",
            "metadata": {"name": "no-strategy", "namespace": test_namespace},
            "spec": {},  # automationStrategyRef is required
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected rejection for missing automationStrategyRef"

    def test_global_configuration_reload_interval_too_short(self, kube_context):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "GlobalConfiguration",
            "metadata": {"name": "global-config"},
            "spec": {"recommendationReloadInterval": "30s"},  # minimum is 1m
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected rejection for interval < 1m"
