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

    def test_cluster_automation_strategy_invalid_bounds_rejected(self, kube_context):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "ClusterAutomationStrategy",
            "metadata": {"name": "cluster-bad-bounds"},
            "spec": {
                "enablement": {
                    "memory": {"limits": {"floor": "2Gi", "ceiling": "1Gi"}}
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

    def test_static_policy_nonexistent_strategy_ref_rejected(self, kube_context, test_namespace):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "StaticPolicy",
            "metadata": {"name": "missing-strategy", "namespace": test_namespace},
            "spec": {"automationStrategyRef": {"name": "does-not-exist"}},
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected rejection for nonexistent AutomationStrategy"
        assert "does-not-exist" in result.stderr

    def test_cluster_static_policy_nonexistent_strategy_ref_rejected(self, kube_context, test_namespace):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "ClusterStaticPolicy",
            "metadata": {"name": "missing-cluster-strategy"},
            "spec": {
                "automationStrategyRef": {"name": "does-not-exist"},
                "scope": {"namespaceSelector": {"operator": "In", "values": [test_namespace]}},
            },
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected rejection for nonexistent ClusterAutomationStrategy"
        assert "does-not-exist" in result.stderr

    def test_container_args_policy_remove_value_rejected(self, kube_context, test_namespace):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "ContainerArgsPolicy",
            "metadata": {"name": "bad-container-args-remove"},
            "spec": {
                "scope": {
                    "namespaceSelector": {"operator": "In", "values": [test_namespace]}
                },
                "containers": {
                    "app": {
                        "args": [{"name": "--flag", "operation": "Remove", "value": "bad"}]
                    }
                },
            },
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected Remove with value to be rejected"
        assert "must be omitted" in result.stderr or "Remove" in result.stderr

    def test_container_args_policy_mixed_operations_rejected(self, kube_context, test_namespace):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "ContainerArgsPolicy",
            "metadata": {"name": "bad-container-args-mixed"},
            "spec": {
                "scope": {
                    "namespaceSelector": {"operator": "In", "values": [test_namespace]}
                },
                "containers": {
                    "app": {
                        "args": [
                            {"name": "--flag"},
                            {"name": "--flag", "operation": "Remove"},
                        ]
                    }
                },
            },
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected mixed operations to be rejected"
        assert "mixed operations" in result.stderr or "operation" in result.stderr

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

    def test_global_configuration_non_singleton_name_rejected(self, kube_context):
        bad = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "GlobalConfiguration",
            "metadata": {"name": "another-global-config"},
            "spec": {},
        }
        result = subprocess.run(
            ["kubectl", "--context", kube_context, "apply", "-f", "-"],
            input=json.dumps(bad),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "Expected rejection for non-singleton GlobalConfiguration"
        assert "global-config" in result.stderr
