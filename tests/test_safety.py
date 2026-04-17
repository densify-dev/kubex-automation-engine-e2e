"""Tests: safety checks — HPA filter and protected namespace enforcement."""

import time

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    automation_strategy_manifest,
    create_deployment,
    delete_deployment,
    get_crd,
    get_deployment_pod,
    get_pod_resources,
    static_policy_manifest,
)


class TestHPAFilter:
    """Verify the controller does not resize pods targeted by an HPA."""

    STRATEGY_NAME = "e2e-hpa-strategy"
    POLICY_NAME = "e2e-hpa-policy"
    DEPLOYMENT = "e2e-hpa-workload"
    HPA_NAME = "e2e-hpa"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "automationstrategies",
            automation_strategy_manifest(self.STRATEGY_NAME, test_namespace),
        )
        create_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT, cpu_request="200m")
        hpa = client.V2HorizontalPodAutoscaler(
            metadata=client.V1ObjectMeta(name=self.HPA_NAME, namespace=test_namespace),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    api_version="apps/v1", kind="Deployment", name=self.DEPLOYMENT
                ),
                min_replicas=1,
                max_replicas=3,
                metrics=[
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="cpu",
                            target=client.V2MetricTarget(
                                type="Utilization", average_utilization=80
                            ),
                        ),
                    )
                ],
            ),
        )
        client.AutoscalingV2Api().create_namespaced_horizontal_pod_autoscaler(test_namespace, hpa)
        yield

        try:
            client.AutoscalingV2Api().delete_namespaced_horizontal_pod_autoscaler(
                self.HPA_NAME, test_namespace
            )
        except ApiException:
            pass
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, test_namespace, plural, name
                )
            except ApiException:
                pass

    def test_cpu_resize_blocked_when_hpa_present(self, k8s_clients, test_namespace):
        policy = static_policy_manifest(
            self.POLICY_NAME,
            test_namespace,
            strategy_name=self.STRATEGY_NAME,
            label_selector_app=self.DEPLOYMENT,
            cpu_request="50m",  # would be a downsize
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "staticpolicies", policy
        )
        time.sleep(5)
        pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
        resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
        assert resources["app"]["requests"].get("cpu") == "200m", (
            "CPU should not have been resized — HPA is protecting this deployment"
        )


class TestProtectedNamespace:
    """Verify the controller refuses to resize workloads in protected namespaces."""

    def test_kube_system_is_protected(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        patterns = gc["spec"].get("protectedNamespacePatterns", [])
        assert any("kube" in p for p in patterns), (
            "kube-* should be in protectedNamespacePatterns by default"
        )

    def test_custom_protected_pattern_persists(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        original = list(gc["spec"].get("protectedNamespacePatterns", []))
        gc["spec"]["protectedNamespacePatterns"] = original + ["test-protected-*"]
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "globalconfigurations", "global-config", gc
        )
        try:
            updated = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            assert "test-protected-*" in updated["spec"]["protectedNamespacePatterns"]
        finally:
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            gc["spec"]["protectedNamespacePatterns"] = original
            k8s_clients.custom.replace_cluster_custom_object(
                GROUP, VERSION, "globalconfigurations", "global-config", gc
            )
