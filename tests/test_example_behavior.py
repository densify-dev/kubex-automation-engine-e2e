"""Tests: vendored examples exercise live cluster behavior, not just schema validity."""

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
from helpers import get_deployment_resources


class TestExampleBehavior:
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
