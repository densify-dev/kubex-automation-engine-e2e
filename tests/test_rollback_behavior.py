"""Tests: live rollback state transitions and cleanup."""

import json
import time

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    ROLLBACK_STATE_ANNOTATION,
    VERSION,
    automation_strategy_manifest,
    create_deployment,
    delete_deployment,
    delete_custom_object,
    get_deployment,
    get_deployment_pod,
    get_pod_resources,
    kubectl,
    rollback_policy_manifest,
    static_policy_manifest,
    wait_for,
)


class TestRollbackBehavior:
    """Verify rollback monitoring, backoff completion, and terminal failure live."""

    STRATEGY_NAME = "e2e-rollback-strategy"
    STATIC_POLICY_NAME = "e2e-rollback-static-policy"
    ROLLBACK_POLICY_NAME = "e2e-rollback-policy"
    PDB_NAME = "e2e-rollback-pdb"
    DEPLOYMENT = "rightsizing-demo"
    NAMESPACE = "default"
    ROLLBACK_REQUESTS_ANNOTATION = "rollbackpolicy.rightsizing.kubex.ai/desired-resource-requests"
    ROLLBACK_LIMITS_ANNOTATION = "rollbackpolicy.rightsizing.kubex.ai/desired-resource-limits"

    INITIAL_RESOURCES = {
        "requests": {"cpu": "250m", "memory": "320Mi"},
        "limits": {"cpu": "400m", "memory": "640Mi"},
    }

    FAILING_RESOURCES = {
        "requests": {"cpu": "300m", "memory": "128Mi"},
        "limits": {"cpu": "600m", "memory": "192Mi"},
    }

    PARTIAL_ADOPTION_RESOURCES = {
        "requests": {"cpu": "275m", "memory": "320Mi"},
        "limits": {"cpu": "425m", "memory": "640Mi"},
    }

    ORIGINAL_RESOURCES = {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "200m", "memory": "512Mi"},
    }

    @pytest.fixture(autouse=True)
    def setup_teardown(self, request, k8s_clients, kube_context):
        suffix = request.node.name.replace("_", "-")[:20]
        self.STRATEGY_NAME = f"e2e-rollback-strategy-{suffix}"
        self.STATIC_POLICY_NAME = f"e2e-rollback-static-{suffix}"
        self.ROLLBACK_POLICY_NAME = f"e2e-rollback-policy-{suffix}"
        self.PDB_NAME = f"e2e-rollback-pdb-{suffix}"
        self.DEPLOYMENT = f"rightsizing-demo-{suffix}"
        self.NAMESPACE = f"e2e-rollback-{suffix}"

        try:
            k8s_clients.core.create_namespace(
                client.V1Namespace(metadata=client.V1ObjectMeta(name=self.NAMESPACE))
            )
        except ApiException as exc:
            if exc.status != 409:
                raise

        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        kubectl(
            "delete",
            "pdb",
            self.PDB_NAME,
            "-n",
            self.NAMESPACE,
            "--ignore-not-found",
            context=kube_context,
        )
        for plural, name in [
            ("rollbackpolicies", self.ROLLBACK_POLICY_NAME),
            ("staticpolicies", self.STATIC_POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            delete_custom_object(k8s_clients.custom, GROUP, VERSION, self.NAMESPACE, plural, name)

        strategy = automation_strategy_manifest(self.STRATEGY_NAME, self.NAMESPACE)
        strategy["spec"]["inPlaceResize"] = {"enabled": False}
        strategy["spec"]["podEviction"] = {"enabled": True}
        strategy["spec"]["safetyChecks"] = {
            "minReadyDuration": "0s",
            "resizeRetryInterval": "5s",
        }
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
            cpu_request=self.ORIGINAL_RESOURCES["requests"]["cpu"],
            mem_request=self.ORIGINAL_RESOURCES["requests"]["memory"],
            cpu_limit=self.ORIGINAL_RESOURCES["limits"]["cpu"],
            mem_limit=self.ORIGINAL_RESOURCES["limits"]["memory"],
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

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "staticpolicies",
            static_policy_manifest(
                self.STATIC_POLICY_NAME,
                self.NAMESPACE,
                self.STRATEGY_NAME,
                label_selector_app=self.DEPLOYMENT,
                cpu_request=self.INITIAL_RESOURCES["requests"]["cpu"],
                mem_request=self.INITIAL_RESOURCES["requests"]["memory"],
                cpu_limit=self.INITIAL_RESOURCES["limits"]["cpu"],
                mem_limit=self.INITIAL_RESOURCES["limits"]["memory"],
            ),
        )

        yield

        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        kubectl(
            "delete",
            "pdb",
            self.PDB_NAME,
            "-n",
            self.NAMESPACE,
            "--ignore-not-found",
            context=kube_context,
            check=False,
        )
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
            {
                "spec": {
                    "monitoringPeriod": monitoring_period,
                }
            },
        )

    def _patch_rollback_policy_threshold(self, k8s_clients, threshold: int) -> None:
        k8s_clients.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            self.ROLLBACK_POLICY_NAME,
            {
                "spec": {
                    "adoptionThresholdPercent": threshold,
                }
            },
        )

    def _patch_static_policy_resources(self, k8s_clients, resources: dict[str, dict[str, str]]) -> None:
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
                                "requests": resources["requests"],
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

    def _rollback_annotations_cleared(self, k8s_clients) -> bool:
        annotations = self._deployment(k8s_clients).metadata.annotations or {}
        return (
            self.ROLLBACK_REQUESTS_ANNOTATION not in annotations
            and self.ROLLBACK_LIMITS_ANNOTATION not in annotations
        )

    def _poke_owner_reconcile(self, k8s_clients) -> None:
        k8s_clients.apps.patch_namespaced_deployment(
            self.DEPLOYMENT,
            self.NAMESPACE,
            {
                "metadata": {
                    "annotations": {
                        "e2e.rightsizing.kubex.ai/poke": str(time.time()),
                    }
                }
            },
        )

    def _event_seen(self, k8s_clients, reason: str, message_substring: str | None = None) -> bool:
        for event in k8s_clients.core.list_namespaced_event(self.NAMESPACE).items:
            involved = event.involved_object
            if not involved:
                continue
            if involved.kind != "Deployment" or involved.name != self.DEPLOYMENT:
                continue
            if event.reason != reason:
                continue
            if message_substring and message_substring not in (event.message or ""):
                continue
            return True
        return False

    def _scale_deployment(self, k8s_clients, replicas: int) -> None:
        k8s_clients.apps.patch_namespaced_deployment(
            self.DEPLOYMENT,
            self.NAMESPACE,
            {"spec": {"replicas": replicas}},
        )

    def _wait_for_ready_replicas(self, k8s_clients, replicas: int, timeout: int = 300) -> None:
        def ready():
            deployment = self._deployment(k8s_clients)
            status = deployment.status
            if status is None:
                return False
            return (status.ready_replicas or 0) == replicas and (status.updated_replicas or 0) == replicas

        wait_for(ready, timeout=timeout, message=f"{replicas} ready replicas")

    def _apply_pdb(self, kube_context: str, max_unavailable: int = 1) -> None:
        kubectl(
            "apply",
            "-f",
            "-",
            context=kube_context,
            input=(
                "apiVersion: policy/v1\n"
                "kind: PodDisruptionBudget\n"
                "metadata:\n"
                f"  name: {self.PDB_NAME}\n"
                f"  namespace: {self.NAMESPACE}\n"
                "spec:\n"
                f"  maxUnavailable: {max_unavailable}\n"
                "  selector:\n"
                "    matchLabels:\n"
                f"      app: {self.DEPLOYMENT}\n"
            ),
        )

    def _count_pods_with_resources(self, k8s_clients, resources: dict[str, dict[str, str]]) -> int:
        count = 0
        pods = k8s_clients.core.list_namespaced_pod(
            self.NAMESPACE,
            label_selector=f"app={self.DEPLOYMENT}",
        ).items
        for pod in pods:
            if pod.metadata.deletion_timestamp is not None:
                continue
            live = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)["app"]
            if (
                live["requests"].get("cpu") == resources["requests"]["cpu"]
                and live["requests"].get("memory") == resources["requests"]["memory"]
                and live["limits"].get("cpu") == resources["limits"]["cpu"]
                and live["limits"].get("memory") == resources["limits"]["memory"]
            ):
                count += 1
        return count

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

    def _wait_for_monitoring_start(self, k8s_clients) -> dict:
        self._wait_for_pod_resources(k8s_clients, self.INITIAL_RESOURCES, timeout=300)
        return self._wait_for_state_mode(k8s_clients, "monitoring", timeout=180)

    def _wait_for_initial_monitoring_success(self, k8s_clients) -> None:
        self._wait_for_monitoring_start(k8s_clients)
        self._wait_for_state_mode(k8s_clients, "monitoringSucceeded", timeout=180)
        wait_for(
            lambda: self._rollback_annotations_cleared(k8s_clients),
            timeout=60,
            message="rollback annotations cleared after monitoring success",
        )
        wait_for(
            lambda: self._event_seen(
                k8s_clients,
                "RollbackMonitoringSucceeded",
                "Completed rollback monitoring window",
            ),
            timeout=60,
            message="rollback monitoring success event",
        )

    def _wait_for_second_monitoring_start(self, k8s_clients) -> None:
        initial_success = self._rollback_state(k8s_clients)
        initial_fingerprint = initial_success.get("activeRecommendationFingerprint") if initial_success else None

        self._patch_rollback_policy_monitoring_period(k8s_clients, "90s")
        self._patch_static_policy_resources(k8s_clients, self.FAILING_RESOURCES)
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

    @pytest.mark.timeout(900)
    def test_rollback_monitoring_succeeds_and_clears_annotations(self, k8s_clients):
        self._wait_for_initial_monitoring_success(k8s_clients)

    @pytest.mark.timeout(900)
    def test_rollback_backoff_completion_clears_annotations(self, k8s_clients):
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._wait_for_second_monitoring_start(k8s_clients)
        self._trigger_organic_failure(k8s_clients, max_attempts=2)

        backed_off = self._wait_for_state_mode(k8s_clients, "backedOff", timeout=180)
        assert backed_off["turn"] == 2
        assert backed_off["failureReason"] in {"oomKilled", "crashLoopBackOff"}
        assert any(reason in backed_off["failureMessage"] for reason in {"OOMKilled", "CrashLoopBackOff"})
        assert self._rollback_annotations_cleared(k8s_clients)

    @pytest.mark.timeout(900)
    def test_rollback_terminal_failure_clears_annotations(self, k8s_clients):
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._wait_for_second_monitoring_start(k8s_clients)
        self._trigger_organic_failure(k8s_clients, max_attempts=1)

        failed = self._wait_for_state_mode(k8s_clients, "failedPermanent", timeout=180)
        assert failed["turn"] == 1
        assert failed["failureReason"] in {"oomKilled", "crashLoopBackOff"}
        assert any(reason in failed["failureMessage"] for reason in {"OOMKilled", "CrashLoopBackOff"})
        assert self._rollback_annotations_cleared(k8s_clients)

    @pytest.mark.timeout(900)
    def test_partial_adoption_succeeds_when_threshold_is_met(self, k8s_clients, kube_context):
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._scale_deployment(k8s_clients, replicas=5)
        self._wait_for_ready_replicas(k8s_clients, replicas=5)
        self._apply_pdb(kube_context, max_unavailable=1)

        self._patch_rollback_policy_monitoring_period(k8s_clients, "5s")
        self._patch_rollback_policy_threshold(k8s_clients, 20)
        self._patch_static_policy_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES)

        self._wait_for_state_mode(k8s_clients, "monitoring", timeout=180)
        wait_for(
            lambda: 0 < self._count_pods_with_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES) < 5,
            timeout=120,
            message="partial adoption observed",
        )
        succeeded = self._wait_for_state_mode(k8s_clients, "monitoringSucceeded", timeout=180)
        adopted = self._count_pods_with_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES)

        assert succeeded["activeRecommendationFingerprint"]
        assert 1 <= adopted < 5

    @pytest.mark.timeout(900)
    def test_partial_adoption_rolls_back_when_threshold_is_not_met(self, k8s_clients, kube_context):
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._scale_deployment(k8s_clients, replicas=5)
        self._wait_for_ready_replicas(k8s_clients, replicas=5)
        self._apply_pdb(kube_context, max_unavailable=1)

        self._patch_rollback_policy_monitoring_period(k8s_clients, "5s")
        self._patch_rollback_policy_threshold(k8s_clients, 100)
        self._patch_static_policy_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES)

        self._wait_for_state_mode(k8s_clients, "monitoring", timeout=180)
        wait_for(
            lambda: 0 < self._count_pods_with_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES) < 5,
            timeout=120,
            message="partial adoption observed before threshold failure",
        )
        threshold_failure = self._wait_for_state_modes(k8s_clients, {"rollingBack", "backingOff", "backedOff"}, timeout=240)

        assert threshold_failure["failureReason"] == "adoptionThresholdNotMet"
        assert "adoption threshold" in threshold_failure["failureMessage"]


def _namespace_gone(k8s_clients, namespace: str) -> bool:
    try:
        k8s_clients.core.read_namespace(namespace)
    except ApiException as exc:
        if exc.status == 404:
            return True
        raise
    return False
