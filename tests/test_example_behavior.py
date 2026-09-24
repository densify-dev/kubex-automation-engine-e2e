"""Tests: vendored examples exercise live cluster behavior, not just schema validity."""

import json
import re
import time

import pytest

from example_utils import (
    EXAMPLES_ROOT,
    all_valid_example_manifests,
    apply_manifest,
    assert_declared_resources_exist,
    delete_manifest_in_reverse,
    skip_reason,
    wait_for_declared_workloads_ready,
)
from helpers import (
    get_crd,
    get_deployment_pod,
    get_deployment_resources,
    get_pod_resources,
    wait_for,
)


class TestExampleBehavior:
    NO_READINESS_GATE_MANIFESTS = {
        EXAMPLES_ROOT / "automationstrategy" / "node-allocatable-headroom.yaml",
    }

    @pytest.mark.timeout(300)
    @pytest.mark.parametrize(
        "manifest_path",
        all_valid_example_manifests(),
        ids=lambda path: path.relative_to(EXAMPLES_ROOT).as_posix(),
    )
    def test_valid_example_manifest_exercises_live_cluster(
        self, manifest_path, kube_context, k8s_clients
    ):
        reason = skip_reason(manifest_path, kube_context)
        if reason:
            pytest.skip(reason)

        try:
            apply_manifest(manifest_path, kube_context)
            assert_declared_resources_exist(manifest_path, kube_context)
            if manifest_path not in self.NO_READINESS_GATE_MANIFESTS:
                wait_for_declared_workloads_ready(manifest_path, k8s_clients)
        finally:
            delete_manifest_in_reverse(manifest_path, kube_context)


class TestHPAExampleBehavior:
    @pytest.mark.parametrize(
        ("manifest_path", "namespace", "deployment", "expected_requests"),
        [
            (
                EXAMPLES_ROOT / "automationstrategy" / "hpa-filter.yaml",
                "automationstrategy-hpa",
                "hpa-demo",
                {"app": {"cpu": "200m", "memory": "256Mi"}},
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "hpa-filter-container.yaml",
                "automationstrategy-hpa-container",
                "hpa-container-demo",
                {
                    "app": {"cpu": "200m", "memory": "256Mi"},
                    "sidecar": {"cpu": "50m", "memory": "64Mi"},
                },
            ),
            (
                EXAMPLES_ROOT / "staticpolicy" / "with-hpa-cpu-filter.yaml",
                "default",
                "rightsizing-demo-hpa-cpu-filter",
                {"demo": {"cpu": "300m", "memory": "512Mi"}},
            ),
        ],
        ids=[
            "automationstrategy/hpa-filter.yaml",
            "automationstrategy/hpa-filter-container.yaml",
            "staticpolicy/with-hpa-cpu-filter.yaml",
        ],
    )
    def test_hpa_example_keeps_workload_requests_unchanged(
        self,
        manifest_path,
        namespace,
        deployment,
        expected_requests,
        kube_context,
        k8s_clients,
    ):
        try:
            apply_manifest(manifest_path, kube_context)
            assert_declared_resources_exist(manifest_path, kube_context)
            wait_for_declared_workloads_ready(manifest_path, k8s_clients)
            time.sleep(20)

            resources = get_deployment_resources(k8s_clients.apps, namespace, deployment)
            for container, expectations in expected_requests.items():
                assert resources[container]["requests"].get("cpu") == expectations["cpu"], (
                    f"expected HPA example {manifest_path.name} to preserve CPU request "
                    f"for container {container}"
                )
                assert resources[container]["requests"].get("memory") == expectations["memory"], (
                    f"expected HPA example {manifest_path.name} to preserve memory request "
                    f"for container {container}"
                )
        finally:
            delete_manifest_in_reverse(manifest_path, kube_context)


class TestMultiPolicyExampleBehavior:
    EXPECTED_RESOURCES = {
        "app": {
            "requests": {"cpu": "250m", "memory": "256Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
        "sidecar": {
            "requests": {"cpu": "150m", "memory": "192Mi"},
            "limits": {"cpu": "300m", "memory": "384Mi"},
        },
        "metrics": {
            "requests": {"cpu": "25m", "memory": "32Mi"},
            "limits": {"cpu": "50m", "memory": "64Mi"},
        },
    }

    @pytest.mark.timeout(900)
    @pytest.mark.parametrize(
        ("manifest_path", "deployment_name", "policy_kind", "policy_prefix", "policy_containers"),
        [
            (
                EXAMPLES_ROOT / "staticpolicy" / "multi-policy-container-scope.yaml",
                "multi-policy-container-scope-demo",
                "StaticPolicy",
                "static",
                {"multi-policy-app": "app", "multi-policy-sidecar": "sidecar"},
            ),
            (
                EXAMPLES_ROOT / "proactivepolicy" / "multi-policy-container-scope.yaml",
                "multi-policy-proactive-container-scope-demo",
                "ProactivePolicy",
                "proactive",
                {
                    "multi-policy-proactive-app": "app",
                    "multi-policy-proactive-sidecar": "sidecar",
                },
            ),
        ],
        ids=[
            "staticpolicy/multi-policy-container-scope.yaml",
            "proactivepolicy/multi-policy-container-scope.yaml",
        ],
    )
    def test_multi_policy_container_scope_composes_policy_results(
        self,
        manifest_path,
        deployment_name,
        policy_kind,
        policy_prefix,
        policy_containers,
        kube_context,
        k8s_clients,
    ):
        global_config = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        if not global_config.get("spec", {}).get("multiPolicyContainerRightsizingEnabled", False):
            pytest.skip("multi-policy container rightsizing is disabled")

        annotation_prefix = f"{policy_prefix}.rightsizing.kubex.ai"
        key_pattern = re.compile(
            rf"^{re.escape(annotation_prefix)}/h[A-Za-z0-9_-]+-desired-resource-(requests|limits)$"
        )

        def recommendation_keys(annotations):
            keys = {"requests": [], "limits": []}
            for key in annotations or {}:
                match = key_pattern.fullmatch(key)
                if match:
                    keys[match.group(1)].append(key)
            return keys

        def annotations_converged():
            deployment = k8s_clients.apps.read_namespaced_deployment(deployment_name, "default")
            keys = recommendation_keys(deployment.metadata.annotations)
            return len(keys["requests"]) == 2 and len(keys["limits"]) == 2

        try:
            apply_manifest(manifest_path, kube_context)
            assert_declared_resources_exist(manifest_path, kube_context)
            wait_for_declared_workloads_ready(manifest_path, k8s_clients)

            def live_pod_converged():
                pod = get_deployment_pod(k8s_clients.core, "default", deployment_name)
                if pod.metadata.deletion_timestamp is not None:
                    return False
                resources = get_pod_resources(k8s_clients.core, "default", pod.metadata.name)
                return all(
                    resources[container][resource_type].get(resource) == value
                    for container, expected in self.EXPECTED_RESOURCES.items()
                    for resource_type, values in expected.items()
                    for resource, value in values.items()
                )

            wait_for(
                live_pod_converged,
                timeout=600,
                message=f"{policy_kind} multi-policy pod resources",
            )
            wait_for(
                annotations_converged,
                timeout=180,
                message=f"{policy_kind} multi-policy deployment annotations",
            )

            deployment = k8s_clients.apps.read_namespaced_deployment(deployment_name, "default")
            annotations = deployment.metadata.annotations or {}
            keys = recommendation_keys(annotations)
            assert len(keys["requests"]) == 2
            assert len(keys["limits"]) == 2
            assert f"{annotation_prefix}/desired-resource-requests" not in annotations
            assert f"{annotation_prefix}/desired-resource-limits" not in annotations

            expected_policy_names = set(policy_containers)
            for usage in ("requests", "limits"):
                payloads = [json.loads(annotations[key]) for key in keys[usage]]
                assert {payload.get("policyName") for payload in payloads} == expected_policy_names
                for payload in payloads:
                    policy_name = payload["policyName"]
                    assert payload["policyNamespace"] == "default"
                    assert payload["policyKind"] == policy_kind
                    assert set(payload["containers"]) == {policy_containers[policy_name]}
        finally:
            delete_manifest_in_reverse(manifest_path, kube_context)
