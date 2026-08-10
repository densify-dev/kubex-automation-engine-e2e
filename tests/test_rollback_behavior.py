"""Tests: live rollback state transitions and cleanup."""

import datetime
import json
import time

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    POLL_INTERVAL,
    ROLLBACK_STATE_ANNOTATION,
    VERSION,
    automation_strategy_manifest,
    create_deployment,
    delete_custom_object,
    delete_deployment,
    get_deployment,
    get_deployment_pod,
    get_pod_resources,
    kubectl,
    namespace_gone,
    pod_is_ready,
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

    UNSCHEDULABLE_RESOURCES = {
        "requests": {"cpu": "80", "memory": "200Gi"},
        "limits": {"cpu": "120", "memory": "240Gi"},
    }

    PARTIAL_ADOPTION_RESOURCES = {
        "requests": {"cpu": "275m", "memory": "320Mi"},
        "limits": {"cpu": "425m", "memory": "640Mi"},
    }

    ORIGINAL_RESOURCES = {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "200m", "memory": "512Mi"},
    }

    @pytest.fixture(params=["eviction", "in-place"], ids=["eviction", "in-place"])
    def rollback_resize_mode(self, request, supports_in_place_resize):
        if request.param == "in-place" and not supports_in_place_resize:
            pytest.skip("rollback in-place tests require Kubernetes v1.33+ in-place resize support")
        return request.param

    @pytest.fixture(autouse=True)
    def setup_teardown(self, request, k8s_clients, kube_context, rollback_resize_mode):
        self.resize_mode = rollback_resize_mode

        suffix = request.node.name.replace("_", "-")[:20]
        self.STRATEGY_NAME = f"e2e-rollback-strategy-{suffix}"
        self.STATIC_POLICY_NAME = f"e2e-rollback-static-{suffix}"
        self.ROLLBACK_POLICY_NAME = f"e2e-rollback-policy-{suffix}"
        self.PDB_NAME = f"e2e-rollback-pdb-{suffix}"
        self.DEPLOYMENT = f"rightsizing-demo-{suffix}"
        self.NAMESPACE = f"e2e-rollback-{suffix}"

        created = False
        try:
            k8s_clients.core.create_namespace(
                client.V1Namespace(metadata=client.V1ObjectMeta(name=self.NAMESPACE))
            )
            created = True
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
        strategy["spec"]["inPlaceResize"] = {"enabled": self.resize_mode == "in-place"}
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
            resize_policy=(
                [client.V1ContainerResizePolicy(resource_name="memory", restart_policy="RestartContainer")]
                if self.resize_mode == "in-place"
                else None
            ),
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
        if created:
            try:
                k8s_clients.core.delete_namespace(self.NAMESPACE)
            except ApiException:
                pass

            wait_for(
                lambda: namespace_gone(k8s_clients, self.NAMESPACE),
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

    def _patch_rollback_policy_target(self, k8s_clients, target: str) -> None:
        k8s_clients.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            self.ROLLBACK_POLICY_NAME,
            {"spec": {"rollbackTarget": target}},
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

    def _event_count(self, k8s_clients, reason: str, message_substring: str | None = None) -> int:
        count = 0
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
            count = max(count, int(event.count or 0))
        return count

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

    def _wait_for_unschedulable_pod(self, k8s_clients, timeout: int):
        pod_box = {"value": None}

        def unschedulable():
            pods = k8s_clients.core.list_namespaced_pod(
                self.NAMESPACE,
                label_selector=f"app={self.DEPLOYMENT}",
            ).items
            for pod in pods:
                if pod.metadata.deletion_timestamp is not None or pod.status.phase != "Pending":
                    continue
                condition = next(
                    (
                        item
                        for item in pod.status.conditions or []
                        if item.type == "PodScheduled"
                        and item.status == "False"
                        and item.reason == "Unschedulable"
                    ),
                    None,
                )
                if condition is None:
                    continue
                live = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)["app"]
                if (
                    live["requests"].get("cpu") == self.UNSCHEDULABLE_RESOURCES["requests"]["cpu"]
                    and live["requests"].get("memory")
                    == self.UNSCHEDULABLE_RESOURCES["requests"]["memory"]
                ):
                    pod_box["value"] = pod
                    return True
            return False

        wait_for(unschedulable, timeout=timeout, message="unschedulable rightsized pod")
        return pod_box["value"]

    def _wait_for_restored_pod(self, k8s_clients, excluded_uids: set[str], timeout: int):
        pod_box = {"value": None}

        def restored():
            pods = k8s_clients.core.list_namespaced_pod(
                self.NAMESPACE,
                label_selector=f"app={self.DEPLOYMENT}",
            ).items
            for pod in pods:
                if (
                    pod.metadata.deletion_timestamp is not None
                    or str(pod.metadata.uid) in excluded_uids
                    or not pod_is_ready(pod)
                ):
                    continue
                live = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)["app"]
                if (
                    live["requests"].get("cpu") == self.INITIAL_RESOURCES["requests"]["cpu"]
                    and live["requests"].get("memory")
                    == self.INITIAL_RESOURCES["requests"]["memory"]
                    and live["limits"].get("cpu") == self.INITIAL_RESOURCES["limits"]["cpu"]
                    and live["limits"].get("memory") == self.INITIAL_RESOURCES["limits"]["memory"]
                ):
                    pod_box["value"] = pod
                    return True
            return False

        wait_for(
            restored,
            timeout=timeout,
            message="replacement pod restored to last successful resources",
        )
        return pod_box["value"]

    def _wait_for_state_mode(self, k8s_clients, mode: str, timeout: int, interval: float = POLL_INTERVAL) -> dict:
        state_box = {"value": None}

        def state_matches():
            state = self._rollback_state(k8s_clients)
            if state and state.get("mode") == mode:
                state_box["value"] = state
                return True
            return False

        wait_for(state_matches, timeout=timeout, interval=interval, message=f"rollback state {mode}")
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

    def _wait_for_same_fingerprint_monitoring_restart(self, k8s_clients) -> None:
        initial_success = self._rollback_state(k8s_clients)
        initial_fingerprint = initial_success.get("activeRecommendationFingerprint") if initial_success else None
        assert initial_fingerprint, "expected initial rollback fingerprint after monitoring success"
        initial_event_count = self._event_count(
            k8s_clients,
            "RollbackMonitoringStarted",
            "Started rollback monitoring for resolved policy",
        )

        current_pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)
        current_name = current_pod.metadata.name
        k8s_clients.core.delete_namespaced_pod(current_name, self.NAMESPACE)

        def replacement_ready():
            pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)
            return pod.metadata.name != current_name and pod_is_ready(pod)

        wait_for(replacement_ready, timeout=180, message="replacement pod after monitoring success")
        self._wait_for_pod_resources(k8s_clients, self.INITIAL_RESOURCES, timeout=180)

        wait_for(
            lambda: self._event_count(
                k8s_clients,
                "RollbackMonitoringStarted",
                "Started rollback monitoring for resolved policy",
            )
            > initial_event_count,
            timeout=180,
            message="same fingerprint monitoring restart event",
        )

        def monitoring_restarted():
            state = self._rollback_state(k8s_clients)
            return bool(
                state
                and state.get("mode") in {"monitoring", "monitoringSucceeded"}
                and state.get("activeRecommendationFingerprint") == initial_fingerprint
            )

        wait_for(monitoring_restarted, timeout=180, message="same fingerprint monitoring restart")

    def _trigger_organic_failure(self, k8s_clients, *, max_attempts: int) -> dict | None:
        self._patch_rollback_policy_backoff(k8s_clients, time_period="10s", max_attempts=max_attempts)

        wait_for(
            lambda: self._deployment_failure_reason(k8s_clients) in {"CrashLoopBackOff", "OOMKilled"},
            timeout=180,
            message="resource-driven failure status",
        )
        self._delete_healthy_pods(k8s_clients)
        self._poke_owner_reconcile(k8s_clients)

        terminal_state = self._wait_for_state_modes(k8s_clients, {"rollingBack", "backingOff", "backedOff"}, timeout=240)
        assert terminal_state["failureReason"] in {"oomKilled", "crashLoopBackOff"}
        assert any(reason in terminal_state["failureMessage"] for reason in {"OOMKilled", "CrashLoopBackOff"})

        self._delete_static_policy(k8s_clients)

        completed = {"value": None}

        def backoff_completed():
            state = self._rollback_state(k8s_clients)
            if state is None:
                return False
            # Don't stop at "backingOff" -- the rollback recommendation is
            # deliberately still present at that point (it survives backingOff
            # so a lower-precedence policy can't undo the rollback before it's
            # proven stable) and annotations aren't cleared until the owner
            # exits backingOff into "backedOff" or "failedPermanent".
            if state.get("mode") in {"backedOff", "failedPermanent"}:
                completed["value"] = state
                return True
            return False

        wait_for(backoff_completed, timeout=600, message="rollback backoff completion")
        return completed["value"]

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
    def test_rollback_monitoring_restarts_for_new_pod_with_same_fingerprint(self, k8s_clients):
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._wait_for_same_fingerprint_monitoring_restart(k8s_clients)

    @pytest.mark.timeout(900)
    def test_rollback_backoff_completion_clears_annotations(self, k8s_clients):
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._wait_for_second_monitoring_start(k8s_clients)
        backed_off = self._trigger_organic_failure(k8s_clients, max_attempts=2)

        if backed_off is not None:
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
    def test_unschedulable_recommendation_rolls_back_to_last_successful(self, k8s_clients):
        if self.resize_mode == "in-place":
            pytest.skip("an impossible request is an eviction-only rollback scenario")

        self._wait_for_initial_monitoring_success(k8s_clients)
        healthy_pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)
        healthy_uid = str(healthy_pod.metadata.uid)
        successful_state = self._rollback_state(k8s_clients)
        successful_resources = successful_state["lastSuccessfulState"]["containers"]["app"]
        assert successful_resources["requests"]["cpu"] == self.INITIAL_RESOURCES["requests"]["cpu"]
        assert (
            successful_resources["requests"]["memory"]
            == self.INITIAL_RESOURCES["requests"]["memory"]
        )
        assert successful_resources["limits"]["cpu"] == self.INITIAL_RESOURCES["limits"]["cpu"]
        assert (
            successful_resources["limits"]["memory"]
            == self.INITIAL_RESOURCES["limits"]["memory"]
        )

        self._patch_rollback_policy_target(k8s_clients, "lastSuccessful")
        self._patch_rollback_policy_monitoring_period(k8s_clients, "10s")
        self._patch_rollback_policy_threshold(k8s_clients, 100)
        self._patch_rollback_policy_backoff(k8s_clients, time_period="5s", max_attempts=1)
        self._patch_static_policy_resources(k8s_clients, self.UNSCHEDULABLE_RESOURCES)

        failed_pod = self._wait_for_unschedulable_pod(k8s_clients, timeout=300)
        failed_uid = str(failed_pod.metadata.uid)
        assert failed_uid != healthy_uid

        wait_for(
            lambda: self._event_seen(k8s_clients, "RollbackRollingBackStarted", "Unschedulable"),
            timeout=180,
            message="unschedulable rollback start event",
        )

        restored_pod = self._wait_for_restored_pod(
            k8s_clients,
            excluded_uids={healthy_uid, failed_uid},
            timeout=300,
        )
        assert str(restored_pod.metadata.uid) not in {healthy_uid, failed_uid}
        terminal = self._wait_for_state_modes(
            k8s_clients,
            {"backingOff", "failedPermanent"},
            timeout=240,
        )
        assert terminal["mode"] != "rollingBack"
        assert terminal["failureReason"] == "unschedulable"
        assert terminal["turn"] == 1

    @pytest.mark.timeout(900)
    def test_unschedulable_recommendation_without_last_successful_state_fails_permanently(self, k8s_clients):
        if self.resize_mode == "in-place":
            pytest.skip("an impossible request is an eviction-only rollback scenario")

        self._wait_for_initial_monitoring_success(k8s_clients)

        self._patch_rollback_policy_target(k8s_clients, "lastSuccessful")
        self._patch_rollback_policy_monitoring_period(k8s_clients, "30s")
        self._patch_rollback_policy_threshold(k8s_clients, 100)
        self._patch_rollback_policy_backoff(k8s_clients, time_period="5s", max_attempts=1)
        self._patch_static_policy_resources(k8s_clients, self.UNSCHEDULABLE_RESOURCES)

        self._wait_for_unschedulable_pod(k8s_clients, timeout=300)
        self._wait_for_state_mode(k8s_clients, "monitoring", timeout=120)

        # Simulate the annotation-only monitoring-promotion path (OwnerRollbackReconciler
        # promoting an owner straight to "monitoring" without ever capturing a
        # LastSuccessfulState snapshot) by stripping the field and forcing the monitoring
        # window to have already expired, then letting the live controller observe the
        # already-unschedulable pod on its next reconcile.
        state = self._rollback_state(k8s_clients)
        assert state is not None
        state.pop("lastSuccessfulState", None)
        state["expiryAt"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        k8s_clients.apps.patch_namespaced_deployment(
            self.DEPLOYMENT,
            self.NAMESPACE,
            {"metadata": {"annotations": {ROLLBACK_STATE_ANNOTATION: json.dumps(state)}}},
        )

        wait_for(
            lambda: self._event_seen(k8s_clients, "RollbackRecommendationUnavailable"),
            timeout=180,
            message="rollback recommendation unavailable event",
        )
        terminal = self._wait_for_state_mode(k8s_clients, "failedPermanent", timeout=120)
        assert terminal["failureReason"] == "rollbackRecommendationUnavailable"
        assert "lastSuccessfulState" not in terminal
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

        self._wait_for_state_mode(k8s_clients, "monitoring", timeout=180, interval=0.2)
        if self.resize_mode == "eviction":
            # A PodDisruptionBudget only throttles voluntary disruptions, so
            # eviction-based resize is naturally paced one pod at a time and
            # a genuine partial-adoption window is reliably observable.
            wait_for(
                lambda: 0 < self._count_pods_with_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES) < 5,
                timeout=120,
                interval=0.2,
                message="partial adoption observed",
            )
        succeeded = self._wait_for_state_mode(k8s_clients, "monitoringSucceeded", timeout=180)
        adopted = self._count_pods_with_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES)

        assert succeeded["activeRecommendationFingerprint"]
        if self.resize_mode == "eviction":
            assert 1 <= adopted < 5
        else:
            # In-place resize applies to every pod immediately with no
            # disruption-based throttle, so all replicas can adopt before any
            # poll observes a partial state. The low threshold is still
            # (over)satisfied -- assert adoption succeeded rather than
            # requiring a specific partial count, which isn't a meaningful or
            # reliably observable state for this resize mode.
            assert 1 <= adopted <= 5

    @pytest.mark.timeout(900)
    def test_partial_adoption_rolls_back_when_threshold_is_not_met(self, k8s_clients, kube_context):
        if self.resize_mode == "in-place":
            pytest.skip(
                "in-place resize has no disruption-based throttle, so every replica adopts "
                "essentially at once -- a 100% threshold is trivially met rather than failed, "
                "making this scenario untestable for this resize mode"
            )
        self._wait_for_initial_monitoring_success(k8s_clients)
        self._scale_deployment(k8s_clients, replicas=5)
        self._wait_for_ready_replicas(k8s_clients, replicas=5)
        self._apply_pdb(kube_context, max_unavailable=1)

        self._patch_rollback_policy_monitoring_period(k8s_clients, "5s")
        self._patch_rollback_policy_threshold(k8s_clients, 100)
        self._patch_static_policy_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES)

        self._wait_for_state_mode(k8s_clients, "monitoring", timeout=180, interval=0.2)
        wait_for(
            lambda: 0 < self._count_pods_with_resources(k8s_clients, self.PARTIAL_ADOPTION_RESOURCES) < 5,
            timeout=120,
            interval=0.2,
            message="partial adoption observed before threshold failure",
        )
        threshold_failure = self._wait_for_state_modes(k8s_clients, {"rollingBack", "backingOff", "backedOff"}, timeout=240)

        assert threshold_failure["failureReason"] == "adoptionThresholdNotMet"
        assert "adoption threshold" in threshold_failure["failureMessage"]
