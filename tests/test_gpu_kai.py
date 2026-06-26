"""GPU scheduling and rebalancing e2e smoke tests."""

import json
from datetime import datetime, timezone

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from example_utils import EXAMPLES_ROOT, apply_manifest, delete_manifest_in_reverse
from helpers import (
    get_crd,
    GROUP,
    ROLLBACK_STATE_ANNOTATION,
    VERSION,
    automation_strategy_manifest,
    create_deployment,
    delete_custom_object,
    delete_deployment,
    get_deployment_pod,
    get_deployment,
    get_pod_resources,
    kubectl,
    rollback_policy_manifest,
    static_policy_manifest,
    prometheus_query,
    wait_for_crd_condition,
    wait_for_pod_ready,
    wait_for,
)


GPU_MANIFEST = EXAMPLES_ROOT / "gpus" / "simple-static-gpu-kai.yaml"
GPU_MIGRATION_MANIFEST = EXAMPLES_ROOT / "gpus" / "simple-static-gpu-vanilla-2kai.yaml"
GPU_CONSOLIDATION_MANIFEST = EXAMPLES_ROOT / "gpus" / "gpu-consolidation-policy.yaml"
GPU_REBALANCING_MANIFEST = EXAMPLES_ROOT / "gpus" / "gpu-rebalancing-policy.yaml"
KUBEAI_MODEL_MANIFEST = EXAMPLES_ROOT / "staticpolicy" / "model.yaml"

pytestmark = pytest.mark.gpu_suite


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

    wait_for(
        has_gpu_recommendations,
        timeout=180,
        message=f"GPU recommendations for {namespace}/{deployment_name}",
    )


def _wait_for_deployment_pod(
    k8s_clients,
    namespace: str,
    deployment_name: str,
    timeout: int = 300,
    exclude_name: str | None = None,
):
    selected_pod = None

    def pod_sort_key(pod):
        return pod.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc)

    def has_pod():
        nonlocal selected_pod
        pods = k8s_clients.core.list_namespaced_pod(
            namespace,
            label_selector=f"app={deployment_name}",
        ).items
        candidates = (
            pods
            if exclude_name is None
            else [pod for pod in pods if pod.metadata.name != exclude_name]
        )
        if candidates:
            selected_pod = max(candidates, key=pod_sort_key)
            return True
        return False

    wait_for(has_pod, timeout=timeout, message=f"pod for {namespace}/{deployment_name}")
    if selected_pod is None:
        raise RuntimeError(f"no pod found for deployment {deployment_name}")
    return selected_pod


def _wait_for_model_pod(k8s_clients, namespace: str, model_name: str, timeout: int = 300):
    selected_pod = None

    def pod_sort_key(pod):
        return pod.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc)

    def has_model_pod():
        nonlocal selected_pod
        pods = k8s_clients.core.list_namespaced_pod(namespace).items
        candidates = []
        for pod in pods:
            owners = pod.metadata.owner_references or []
            if any(owner.kind == "Model" and owner.name == model_name for owner in owners):
                candidates.append(pod)
        if candidates:
            selected_pod = max(candidates, key=pod_sort_key)
            return True
        return False

    wait_for(has_model_pod, timeout=timeout, message=f"pod for model {namespace}/{model_name}")
    if selected_pod is None:
        raise RuntimeError(f"no pod found for model {model_name}")
    return selected_pod


def _wait_for_prometheus_ready(k8s_clients):
    wait_for_pod_ready(k8s_clients.core, "monitoring", "app=prometheus", timeout=300)


def _wait_for_prometheus_series(
    kube_context: str,
    k8s_clients,
    namespace: str,
    deployment_name: str,
    metric_name: str,
):
    pod = get_deployment_pod(k8s_clients.core, namespace, deployment_name)

    def has_series():
        query = f'{metric_name}{{namespace="{namespace}",pod="{pod.metadata.name}",container="app"}}'
        result = prometheus_query(kube_context, "monitoring", query)
        return bool(result["data"]["result"])

    wait_for(
        has_series,
        timeout=180,
        message=f"Prometheus series for {metric_name} in {namespace}/{deployment_name}",
    )


@pytest.mark.usefixtures("kind_cluster")
class TestGpuKai:
    @pytest.mark.timeout(900)
    def test_kubeai_model_example_mutates_for_vllm_gpu(self, kube_context, k8s_clients):
        try:
            _wait_for_global_configuration_ready(k8s_clients)
            apply_manifest(KUBEAI_MODEL_MANIFEST, kube_context)

            annotated_pod = None

            def model_pod_mutated() -> bool:
                nonlocal annotated_pod
                pods = k8s_clients.core.list_namespaced_pod("default").items
                for pod in pods:
                    owners = pod.metadata.owner_references or []
                    if not any(owner.kind == "Model" and owner.name == "kubeai-demo-model" for owner in owners):
                        continue
                    if (pod.metadata.annotations or {}).get("gpu-fraction") == "0.7":
                        annotated_pod = pod
                        return True
                return False

            wait_for(model_pod_mutated, timeout=300, message="kubeai model pod gpu-fraction to update")
            pod = annotated_pod
            assert pod is not None
            resources = get_pod_resources(k8s_clients.core, "default", pod.metadata.name)
            container = pod.spec.containers[0]
            assert container.args is not None
            assert "--gpu-memory-utilization=0.9" in container.args
            assert pod.metadata.annotations.get("gpu-fraction") == "0.7"
            assert resources[container.name]["requests"].get("cpu") == "500m"
            assert resources[container.name]["limits"].get("cpu") == "1"
            assert resources[container.name]["requests"].get("nvidia.com/gpu") is None
            assert resources[container.name]["limits"].get("nvidia.com/gpu") is None
        finally:
            delete_manifest_in_reverse(KUBEAI_MODEL_MANIFEST, kube_context)

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
            _wait_for_deployment_pod(
                k8s_clients,
                "gpu-kai",
                "gpu-kai-demo",
                exclude_name=current_pod.metadata.name,
            )
            annotated_pod = None

            def gpu_fraction_ready() -> bool:
                nonlocal annotated_pod
                pod = get_deployment_pod(k8s_clients.core, "gpu-kai", "gpu-kai-demo")
                if (pod.metadata.annotations or {}).get("gpu-fraction") == "0.7":
                    annotated_pod = pod
                    return True
                return False

            wait_for(
                gpu_fraction_ready,
                timeout=180,
                message="gpu-kai pod gpu-fraction to update",
            )
            pod = annotated_pod
            assert pod is not None
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
            _wait_for_deployment_pod(k8s_clients, "gpu-kai", "gpu-kai-demo")
            _wait_for_prometheus_ready(k8s_clients)
            for metric_name in (
                "kubex_gpu_container_sm_utilization_percent",
                "kubex_gpu_container_memory_footprint_percent",
            ):
                _wait_for_prometheus_series(
                    kube_context,
                    k8s_clients,
                    "gpu-kai",
                    "gpu-kai-demo",
                    metric_name,
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


class TestGpuKaiRollback:
    STRATEGY_NAME = "e2e-kai-rollback-strategy"
    STATIC_POLICY_NAME = "e2e-kai-rollback-static-policy"
    ROLLBACK_POLICY_NAME = "e2e-kai-rollback-policy"
    DEPLOYMENT = "gpu-kai-rollback-demo"
    NAMESPACE = "gpu-kai-rollback"

    INITIAL_RESOURCES = {
        "requests": {"cpu": "250m", "memory": "320Mi"},
        "limits": {"cpu": "400m", "memory": "640Mi"},
    }

    FAILING_RESOURCES = {
        "requests": {"cpu": "300m", "memory": "128Mi"},
        "limits": {"cpu": "600m", "memory": "192Mi"},
    }

    @pytest.fixture(autouse=True)
    def setup_teardown(self, request, k8s_clients, kube_context):
        suffix = request.node.name.replace("_", "-")[:20]
        self.STRATEGY_NAME = f"e2e-kai-rollback-strategy-{suffix}"
        self.STATIC_POLICY_NAME = f"e2e-kai-rollback-static-{suffix}"
        self.ROLLBACK_POLICY_NAME = f"e2e-kai-rollback-policy-{suffix}"
        self.DEPLOYMENT = f"gpu-kai-rollback-demo-{suffix}"
        self.NAMESPACE = f"gpu-kai-rollback-{suffix}"

        try:
            k8s_clients.core.create_namespace(
                client.V1Namespace(metadata=client.V1ObjectMeta(name=self.NAMESPACE))
            )
        except ApiException as exc:
            if exc.status != 409:
                raise

        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        for plural, name in [
            ("rollbackpolicies", self.ROLLBACK_POLICY_NAME),
            ("staticpolicies", self.STATIC_POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            delete_custom_object(k8s_clients.custom, GROUP, VERSION, self.NAMESPACE, plural, name)

        strategy = automation_strategy_manifest(self.STRATEGY_NAME, self.NAMESPACE)
        strategy["spec"]["enablement"]["overrideScheduler"] = "kai"
        strategy["spec"]["podEviction"] = {"enabled": True}
        strategy["spec"]["inPlaceResize"] = {"enabled": False}
        strategy["spec"]["safetyChecks"] = {
            "minReadyDuration": "0s",
            "resizeRetryInterval": "5s",
        }
        strategy["spec"]["experimental"] = {"gpuKaiContract": "v1alpha1-2026-04"}
        strategy["spec"]["kai"] = {"setQueueWhenSpecified": False}
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "automationstrategies",
            strategy,
        )

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            rollback_policy_manifest(
                self.ROLLBACK_POLICY_NAME,
                self.NAMESPACE,
                label_selector_app=self.DEPLOYMENT,
                monitoring_period="20s",
                weight=100,
                backoff={
                    "timePeriod": "10s",
                    "multiplyByTurn": 1,
                    "maxAttempts": 2,
                },
            ),
        )

        create_deployment(
            k8s_clients.apps,
            self.NAMESPACE,
            self.DEPLOYMENT,
            cpu_request=self.INITIAL_RESOURCES["requests"]["cpu"],
            mem_request=self.INITIAL_RESOURCES["requests"]["memory"],
            cpu_limit=self.INITIAL_RESOURCES["limits"]["cpu"],
            mem_limit=self.INITIAL_RESOURCES["limits"]["memory"],
            image="python:3.12-alpine",
            command=["python", "-c"],
            args=[
                "import time\n"
                "chunks = []\n"
                "while len(chunks) < 220:\n"
                "    chunks.append(bytearray(1024 * 1024))\n"
                "    time.sleep(0.02)\n"
                "time.sleep(3600)\n",
            ],
            resize_policy=[
                client.V1ContainerResizePolicy(resource_name="memory", restart_policy="RestartContainer")
            ],
        )

        static_policy = static_policy_manifest(
            self.STATIC_POLICY_NAME,
            self.NAMESPACE,
            self.STRATEGY_NAME,
            label_selector_app=self.DEPLOYMENT,
            cpu_request=self.INITIAL_RESOURCES["requests"]["cpu"],
            mem_request=self.INITIAL_RESOURCES["requests"]["memory"],
            cpu_limit=self.INITIAL_RESOURCES["limits"]["cpu"],
            mem_limit=self.INITIAL_RESOURCES["limits"]["memory"],
        )
        static_policy["spec"]["resources"]["containers"]["*"]["requests"]["gpu"] = "0.25"
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "staticpolicies",
            static_policy,
        )

        yield

        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        for plural, name in [
            ("rollbackpolicies", self.ROLLBACK_POLICY_NAME),
            ("staticpolicies", self.STATIC_POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            delete_custom_object(k8s_clients.custom, GROUP, VERSION, self.NAMESPACE, plural, name)
        try:
            k8s_clients.core.delete_namespace(self.NAMESPACE)
        except ApiException:
            pass

        wait_for(
            lambda: _namespace_gone(k8s_clients, self.NAMESPACE),
            timeout=120,
            message=f"namespace {self.NAMESPACE} deletion",
        )

    def _deployment(self, k8s_clients):
        return get_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)

    def _rollback_state(self, k8s_clients) -> dict | None:
        annotations = self._deployment(k8s_clients).metadata.annotations or {}
        raw = annotations.get(ROLLBACK_STATE_ANNOTATION)
        if not raw:
            return None
        return json.loads(raw)

    def _rollback_annotations_cleared(self, k8s_clients) -> bool:
        annotations = self._deployment(k8s_clients).metadata.annotations or {}
        return (
            "rollbackpolicy.rightsizing.kubex.ai/desired-resource-requests" not in annotations
            and "rollbackpolicy.rightsizing.kubex.ai/desired-resource-limits" not in annotations
        )

    def _patch_rollback_policy_backoff(self, k8s_clients, *, time_period: str, max_attempts: int) -> None:
        k8s_clients.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            self.ROLLBACK_POLICY_NAME,
            {
                "spec": {
                    "backoff": {
                        "timePeriod": time_period,
                        "multiplyByTurn": 1,
                        "maxAttempts": max_attempts,
                    }
                }
            },
        )

    def _patch_rollback_policy_monitoring_period(self, k8s_clients, monitoring_period: str) -> None:
        k8s_clients.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            self.ROLLBACK_POLICY_NAME,
            {"spec": {"monitoringPeriod": monitoring_period}},
        )

    def _patch_static_policy_resources(self, k8s_clients, resources: dict[str, dict[str, str]], gpu_fraction: str) -> None:
        k8s_clients.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "staticpolicies",
            self.STATIC_POLICY_NAME,
            {
                "spec": {
                    "resources": {
                        "containers": {
                            "*": {
                                "requests": {**resources["requests"], "gpu": gpu_fraction},
                                "limits": resources["limits"],
                            }
                        }
                    }
                }
            },
        )

    def _delete_static_policy(self, k8s_clients) -> None:
        delete_custom_object(
            k8s_clients.custom,
            GROUP,
            VERSION,
            self.NAMESPACE,
            "staticpolicies",
            self.STATIC_POLICY_NAME,
        )

    def _wait_for_pod_resources(self, k8s_clients, resources: dict[str, dict[str, str]], timeout: int) -> None:
        def applied():
            pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)
            live = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)["app"]
            return (
                live["requests"].get("cpu") == resources["requests"]["cpu"]
                and live["requests"].get("memory") == resources["requests"]["memory"]
                and live["limits"].get("cpu") == resources["limits"]["cpu"]
                and live["limits"].get("memory") == resources["limits"]["memory"]
            )

        wait_for(applied, timeout=timeout, message="live resource update")

    def _wait_for_state_mode(self, k8s_clients, mode: str, timeout: int) -> dict:
        state_box = {"value": None}

        def state_matches():
            state = self._rollback_state(k8s_clients)
            if state and state.get("mode") == mode:
                state_box["value"] = state
                return True
            return False

        wait_for(state_matches, timeout=timeout, message=f"rollback state {mode}")
        return state_box["value"]

    def _wait_for_state_modes(self, k8s_clients, modes: set[str], timeout: int) -> dict:
        state_box = {"value": None}

        def state_matches():
            state = self._rollback_state(k8s_clients)
            if state and state.get("mode") in modes:
                state_box["value"] = state
                return True
            return False

        wait_for(state_matches, timeout=timeout, message=f"rollback state in {sorted(modes)}")
        return state_box["value"]

    def _wait_for_initial_monitoring_success(self, k8s_clients) -> None:
        self._wait_for_pod_resources(k8s_clients, self.INITIAL_RESOURCES, timeout=300)

        def pod_fraction_applied():
            pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)
            return (pod.metadata.annotations or {}).get("gpu-fraction") == "0.25"

        wait_for(pod_fraction_applied, timeout=180, message="kai gpu-fraction annotation")
        self._wait_for_state_mode(k8s_clients, "monitoring", timeout=180)
        self._wait_for_state_mode(k8s_clients, "monitoringSucceeded", timeout=180)
        wait_for(
            lambda: self._rollback_annotations_cleared(k8s_clients),
            timeout=60,
            message="rollback annotations cleared after monitoring success",
        )

    def _wait_for_second_monitoring_start(self, k8s_clients) -> None:
        initial_success = self._rollback_state(k8s_clients)
        initial_fingerprint = initial_success.get("activeRecommendationFingerprint") if initial_success else None

        self._patch_rollback_policy_monitoring_period(k8s_clients, "90s")
        self._patch_static_policy_resources(k8s_clients, self.FAILING_RESOURCES, gpu_fraction="0.25")
        self._wait_for_pod_resources(k8s_clients, self.FAILING_RESOURCES, timeout=300)

        def second_monitoring_started():
            state = self._rollback_state(k8s_clients)
            return bool(
                state
                and state.get("mode") == "monitoring"
                and state.get("activeRecommendationFingerprint")
                and state.get("activeRecommendationFingerprint") != initial_fingerprint
            )

        wait_for(second_monitoring_started, timeout=180, message="second rollback monitoring start")

    def _trigger_organic_failure(self, k8s_clients, *, max_attempts: int) -> None:
        self._patch_rollback_policy_backoff(k8s_clients, time_period="10s", max_attempts=max_attempts)

        wait_for(
            lambda: self._deployment_failure_reason(k8s_clients) in {"CrashLoopBackOff", "OOMKilled"},
            timeout=180,
            message="resource-driven failure status",
        )
        self._delete_healthy_pods(k8s_clients)
        self._poke_owner_reconcile(k8s_clients)

        rolling_back = self._wait_for_state_mode(k8s_clients, "rollingBack", timeout=240)
        assert rolling_back["failureReason"] in {"oomKilled", "crashLoopBackOff"}
        assert any(reason in rolling_back["failureMessage"] for reason in {"OOMKilled", "CrashLoopBackOff"})

        self._delete_static_policy(k8s_clients)
        self._wait_for_state_mode(k8s_clients, "backingOff", timeout=240)

    def _deployment_failure_reason(self, k8s_clients) -> str | None:
        pods = k8s_clients.core.list_namespaced_pod(
            self.NAMESPACE,
            label_selector=f"app={self.DEPLOYMENT}",
        ).items
        for pod in pods:
            for status in pod.status.container_statuses or []:
                if status.name != "app" or status.state is None:
                    continue
                if status.state.waiting is not None:
                    return status.state.waiting.reason
                if status.state.terminated is not None:
                    return status.state.terminated.reason
        return None

    def _delete_healthy_pods(self, k8s_clients) -> None:
        pods = k8s_clients.core.list_namespaced_pod(
            self.NAMESPACE,
            label_selector=f"app={self.DEPLOYMENT}",
        ).items
        for pod in pods:
            statuses = pod.status.container_statuses or []
            if any(
                status.state
                and (
                    (status.state.waiting and status.state.waiting.reason == "CrashLoopBackOff")
                    or (status.state.terminated and status.state.terminated.reason == "OOMKilled")
                )
                for status in statuses
            ):
                continue
            try:
                k8s_clients.core.delete_namespaced_pod(pod.metadata.name, self.NAMESPACE)
            except ApiException as exc:
                if exc.status != 404:
                    raise

    def _poke_owner_reconcile(self, k8s_clients) -> None:
        k8s_clients.apps.patch_namespaced_deployment(
            self.DEPLOYMENT,
            self.NAMESPACE,
            {"metadata": {"annotations": {"e2e.rightsizing.kubex.ai/poke": "rollback-kai"}}},
        )

    @pytest.mark.timeout(900)
    def test_kai_rollback_backoff_completion_clears_annotations(self, k8s_clients):
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._wait_for_second_monitoring_start(k8s_clients)
        self._trigger_organic_failure(k8s_clients, max_attempts=2)

        backed_off = self._wait_for_state_mode(k8s_clients, "backedOff", timeout=180)
        assert backed_off["turn"] == 2
        assert backed_off["failureReason"] in {"oomKilled", "crashLoopBackOff"}
        assert any(reason in backed_off["failureMessage"] for reason in {"OOMKilled", "CrashLoopBackOff"})
        assert self._rollback_annotations_cleared(k8s_clients)

        def gpu_fraction_restored():
            pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)
            return (pod.metadata.annotations or {}).get("gpu-fraction") == "0.25"

        wait_for(gpu_fraction_restored, timeout=180, message="kai gpu-fraction restored after rollback")


def _namespace_gone(k8s_clients, namespace: str) -> bool:
    try:
        k8s_clients.core.read_namespace(namespace)
    except ApiException as exc:
        if exc.status == 404:
            return True
        raise
    return False
