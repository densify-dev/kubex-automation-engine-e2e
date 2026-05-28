"""GPU scheduling and rebalancing e2e smoke tests."""

import pytest

from example_utils import EXAMPLES_ROOT, apply_manifest, delete_manifest_in_reverse
from helpers import (
    get_crd,
    get_deployment_pod,
    get_pod_resources,
    kubectl,
    prometheus_query,
    wait_for_crd_condition,
    wait_for_pod_ready,
    wait_for,
)


GPU_MANIFEST = EXAMPLES_ROOT / "gpus" / "simple-static-gpu-kai.yaml"
GPU_MIGRATION_MANIFEST = EXAMPLES_ROOT / "gpus" / "simple-static-gpu-vanilla-2kai.yaml"
GPU_CONSOLIDATION_MANIFEST = EXAMPLES_ROOT / "gpus" / "gpu-consolidation-policy.yaml"
GPU_REBALANCING_MANIFEST = EXAMPLES_ROOT / "gpus" / "gpu-rebalancing-policy.yaml"


def _wait_for_global_configuration_ready(k8s_clients):
    def is_ready():
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        conditions = gc.get("status", {}).get("conditions", [])
        if any(c.get("type") == "PodAdmissionWebhookHealthy" and c.get("status") == "True" for c in conditions):
            return True
        return gc.get("status", {}).get("webhookHealth", {}).get("lastProbeResult") == "Success"

    wait_for(is_ready, timeout=120, message="GlobalConfiguration webhook health to be confirmed")


def _wait_for_gpu_recommendations(k8s_clients, namespace: str, deployment_name: str):
    def has_gpu_recommendations():
        pod = get_deployment_pod(k8s_clients.core, namespace, deployment_name)
        annotations = pod.metadata.annotations or {}
        return (
            "static.rightsizing.kubex.ai/desired-resource-requests" in annotations
            and "static.rightsizing.kubex.ai/desired-resource-limits" in annotations
        )

    wait_for(has_gpu_recommendations, timeout=180, message=f"GPU recommendations for {namespace}/{deployment_name}")


def _wait_for_deployment_pod(
    k8s_clients,
    namespace: str,
    deployment_name: str,
    timeout: int = 300,
    exclude_name: str | None = None,
):
    def has_pod():
        pods = k8s_clients.core.list_namespaced_pod(namespace, label_selector=f"app={deployment_name}").items
        if exclude_name is None:
            return bool(pods)
        return any(pod.metadata.name != exclude_name for pod in pods)

    wait_for(has_pod, timeout=timeout, message=f"pod for {namespace}/{deployment_name}")
    return get_deployment_pod(k8s_clients.core, namespace, deployment_name)


def _wait_for_prometheus_ready(k8s_clients):
    wait_for_pod_ready(k8s_clients.core, "monitoring", "app=prometheus", timeout=300)


def _wait_for_prometheus_series(kube_context: str, query: str):
    def has_series():
        result = prometheus_query(kube_context, "monitoring", query)
        return bool(result["data"]["result"])

    wait_for(has_series, timeout=180, message=f"Prometheus series for {query}")


@pytest.mark.usefixtures("kind_cluster")
class TestGpuKai:
    @pytest.mark.timeout(900)
    def test_gpu_example_mutates_for_kai(self, kube_context, k8s_clients):
        try:
            _wait_for_global_configuration_ready(k8s_clients)
            apply_manifest(GPU_MANIFEST, kube_context)
            _wait_for_gpu_recommendations(k8s_clients, "gpu-kai", "gpu-kai-demo")
            current_pod = _wait_for_deployment_pod(k8s_clients, "gpu-kai", "gpu-kai-demo")
            kubectl(
                "delete",
                "pod",
                "-n",
                "gpu-kai",
                current_pod.metadata.name,
                "--ignore-not-found",
                context=kube_context,
            )
            pod = _wait_for_deployment_pod(k8s_clients, "gpu-kai", "gpu-kai-demo", exclude_name=current_pod.metadata.name)
            resources = get_pod_resources(k8s_clients.core, "gpu-kai", pod.metadata.name)
            assert pod.metadata.labels.get("kai.scheduler/queue") == "my-queue"
            assert pod.metadata.annotations.get("gpu-fraction") == "0.7"
            assert resources["app"]["requests"].get("cpu") == "200m"
            assert resources["app"]["limits"].get("cpu") == "600m"
            assert resources["app"]["requests"].get("nvidia.com/gpu") is None
            assert resources["app"]["limits"].get("nvidia.com/gpu") is None
        finally:
            delete_manifest_in_reverse(GPU_MANIFEST, kube_context)

    @pytest.mark.timeout(900)
    def test_gpu_vanilla_example_migrates_to_kai(self, kube_context, k8s_clients):
        try:
            _wait_for_global_configuration_ready(k8s_clients)
            apply_manifest(GPU_MIGRATION_MANIFEST, kube_context)
            _wait_for_gpu_recommendations(k8s_clients, "gpu-vanilla-2kai", "gpu-vanilla-2kai-demo")
            current_pod = _wait_for_deployment_pod(k8s_clients, "gpu-vanilla-2kai", "gpu-vanilla-2kai-demo")
            kubectl(
                "delete",
                "pod",
                "-n",
                "gpu-vanilla-2kai",
                current_pod.metadata.name,
                "--ignore-not-found",
                context=kube_context,
            )
            pod = _wait_for_deployment_pod(k8s_clients, "gpu-vanilla-2kai", "gpu-vanilla-2kai-demo", exclude_name=current_pod.metadata.name)
            resources = get_pod_resources(
                k8s_clients.core,
                "gpu-vanilla-2kai",
                pod.metadata.name,
            )
            assert resources["app"]["requests"].get("nvidia.com/gpu") is None
            assert resources["app"]["limits"].get("nvidia.com/gpu") is None
        finally:
            delete_manifest_in_reverse(GPU_MIGRATION_MANIFEST, kube_context)

    @pytest.mark.timeout(900)
    def test_gpu_example_emits_gpu_metrics(self, kube_context, k8s_clients):
        try:
            apply_manifest(GPU_MANIFEST, kube_context)
            pod = _wait_for_deployment_pod(k8s_clients, "gpu-kai", "gpu-kai-demo")
            _wait_for_prometheus_ready(k8s_clients)
            for metric_name in (
                "kubex_gpu_container_sm_utilization_percent",
                "kubex_gpu_container_memory_utilization_percent",
            ):
                _wait_for_prometheus_series(
                    kube_context,
                    f'{metric_name}{{namespace="gpu-kai",pod="{pod.metadata.name}",container="app"}}',
                )
        finally:
            delete_manifest_in_reverse(GPU_MANIFEST, kube_context)

    @pytest.mark.timeout(900)
    def test_gpu_consolidation_policy_reaches_status(self, kube_context, k8s_clients):
        policy_name = "gpu-consolidation-pool-a"
        _wait_for_global_configuration_ready(k8s_clients)
        nodes = [node.metadata.name for node in k8s_clients.core.list_node().items if node.metadata.name]
        worker_node = next((name for name in nodes if "worker" in name), nodes[0])
        try:
            kubectl("label", "node", worker_node, "kubex.ai/gpu-pool=pool-a", "--overwrite", context=kube_context)
            apply_manifest(GPU_CONSOLIDATION_MANIFEST, kube_context)
            wait_for_crd_condition(
                k8s_clients.custom,
                "gpuconsolidationpolicies",
                policy_name,
                "Available",
                namespace=None,
                predicate=lambda condition: condition.get("reason") == "NoCandidate",
                timeout=180,
            )
        finally:
            delete_manifest_in_reverse(GPU_CONSOLIDATION_MANIFEST, kube_context)
            try:
                kubectl("label", "node", worker_node, "kubex.ai/gpu-pool-", context=kube_context)
            except Exception:
                pass

    @pytest.mark.timeout(900)
    def test_gpu_rebalancing_policy_reaches_status(self, kube_context, k8s_clients):
        policy_name = "gpu-rebalancing-policy"
        try:
            _wait_for_global_configuration_ready(k8s_clients)
            apply_manifest(GPU_MIGRATION_MANIFEST, kube_context)
            apply_manifest(GPU_REBALANCING_MANIFEST, kube_context)

            wait_for_crd_condition(
                k8s_clients.custom,
                "gpurebalancingpolicies",
                policy_name,
                "AutomationStrategyResolved",
                namespace="default",
                predicate=lambda condition: condition.get("status") == "True" and condition.get("reason") == "Resolved",
                timeout=600,
            )
        finally:
            delete_manifest_in_reverse(GPU_REBALANCING_MANIFEST, kube_context)
            delete_manifest_in_reverse(GPU_MIGRATION_MANIFEST, kube_context)
