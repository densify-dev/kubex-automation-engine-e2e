"""Tests: live rollback state transitions and cleanup."""

import json

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    ROLLBACK_STATE_ANNOTATION,
    VERSION,
    automation_strategy_manifest,
    create_deployment,
    delete_deployment,
    get_deployment,
    get_deployment_pod,
    get_pod_resources,
    patch_pod_status,
    rollback_policy_manifest,
    static_policy_manifest,
    wait_for,
)


class TestRollbackBehavior:
    """Verify rollback monitoring, backoff completion, and terminal failure in a live cluster."""

    STRATEGY_NAME = "e2e-rollback-strategy"
    STATIC_POLICY_NAME = "e2e-rollback-static-policy"
    ROLLBACK_POLICY_NAME = "e2e-rollback-policy"
    DEPLOYMENT = "rightsizing-demo"
    NAMESPACE = "default"

    INITIAL_RESOURCES = {
        "requests": {"cpu": "250m", "memory": "256Mi"},
        "limits": {"cpu": "400m", "memory": "512Mi"},
    }

    UPDATED_RESOURCES = {
        "requests": {"cpu": "300m", "memory": "320Mi"},
        "limits": {"cpu": "600m", "memory": "640Mi"},
    }

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients):
        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        for plural, name in [
            ("rollbackpolicies", self.ROLLBACK_POLICY_NAME),
            ("staticpolicies", self.STATIC_POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, plural, name
                )
            except ApiException:
                pass

        strategy = automation_strategy_manifest(self.STRATEGY_NAME, self.NAMESPACE)
        strategy["spec"]["inPlaceResize"] = {"enabled": True}
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

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            rollback_policy_manifest(
                self.ROLLBACK_POLICY_NAME,
                self.NAMESPACE,
                label_selector_app=self.DEPLOYMENT,
                monitoring_period="1m",
                backoff={"timePeriod": "1m", "multiplyByTurn": 1, "maxAttempts": 2},
            ),
        )

        create_deployment(
            k8s_clients.apps,
            self.NAMESPACE,
            self.DEPLOYMENT,
            cpu_request="100m",
            mem_request="128Mi",
            cpu_limit="200m",
            mem_limit="256Mi",
        )

        yield

        delete_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)
        for plural, name in [
            ("rollbackpolicies", self.ROLLBACK_POLICY_NAME),
            ("staticpolicies", self.STATIC_POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, plural, name
                )
            except ApiException:
                pass

    def _deployment(self, k8s_clients):
        return get_deployment(k8s_clients.apps, self.NAMESPACE, self.DEPLOYMENT)

    def _rollback_state(self, k8s_clients) -> dict | None:
        annotations = self._deployment(k8s_clients).metadata.annotations or {}
        raw = annotations.get(ROLLBACK_STATE_ANNOTATION)
        if not raw:
            return None
        return json.loads(raw)

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

    def _patch_rollback_state(self, k8s_clients, state: dict) -> None:
        k8s_clients.apps.patch_namespaced_deployment(
            self.DEPLOYMENT,
            self.NAMESPACE,
            {
                "metadata": {
                    "annotations": {
                        ROLLBACK_STATE_ANNOTATION: json.dumps(state),
                    }
                }
            },
        )

    def _patch_deployment_pod_status(self, k8s_clients, status: dict) -> None:
        pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, self.DEPLOYMENT)
        patch_pod_status(k8s_clients.core, self.NAMESPACE, pod.metadata.name, status)

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

    @pytest.mark.timeout(1200)
    def test_monitoring_reseeds_for_new_recommendation_then_succeeds(
        self, k8s_clients
    ):
        self._wait_for_pod_resources(k8s_clients, self.INITIAL_RESOURCES, timeout=300)

        def monitoring_seeded():
            state = self._rollback_state(k8s_clients)
            return bool(state and state.get("mode") == "monitoring" and state.get("activeRecommendationFingerprint"))

        wait_for(monitoring_seeded, timeout=180, message="initial rollback monitoring seed")
        initial_state = self._rollback_state(k8s_clients)
        assert initial_state is not None
        initial_fingerprint = initial_state["activeRecommendationFingerprint"]

        self._patch_static_policy_resources(k8s_clients, self.UPDATED_RESOURCES)
        self._wait_for_pod_resources(k8s_clients, self.UPDATED_RESOURCES, timeout=300)

        def reseeded():
            state = self._rollback_state(k8s_clients)
            return bool(
                state
                and state.get("mode") == "monitoring"
                and state.get("activeRecommendationFingerprint") != initial_fingerprint
            )

        wait_for(reseeded, timeout=180, message="rollback reseed for new recommendation")
        reseeded_state = self._rollback_state(k8s_clients)
        assert reseeded_state is not None
        assert reseeded_state["activeRecommendationFingerprint"] != initial_fingerprint

        def monitoring_succeeded_and_cleared():
            state = self._rollback_state(k8s_clients)
            if not state:
                return False
            if state.get("mode") != "monitoringSucceeded":
                return False
            annotations = self._deployment(k8s_clients).metadata.annotations or {}
            return (
                "rollback.rightsizing.kubex.ai/requests" not in annotations
                and "rollback.rightsizing.kubex.ai/limits" not in annotations
            )

        wait_for(
            monitoring_succeeded_and_cleared,
            timeout=240,
            message="rollback monitoring success after monitoring period",
        )

    @pytest.mark.timeout(900)
    def test_monitoring_failure_transitions_to_rolling_back_then_backing_off(self, k8s_clients):
        self._wait_for_pod_resources(k8s_clients, self.INITIAL_RESOURCES, timeout=300)

        def monitoring_seeded():
            state = self._rollback_state(k8s_clients)
            return bool(state and state.get("mode") == "monitoring")

        wait_for(monitoring_seeded, timeout=180, message="initial rollback monitoring seed")

        self._patch_deployment_pod_status(
            k8s_clients,
            {
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                    }
                ],
            },
        )

        def rolling_back():
            state = self._rollback_state(k8s_clients)
            return bool(state and state.get("mode") == "rollingBack")

        wait_for(rolling_back, timeout=180, message="rollback enters rollingBack after failure")

        def backing_off():
            state = self._rollback_state(k8s_clients)
            return bool(state and state.get("mode") == "backingOff")

        wait_for(backing_off, timeout=240, message="rollback enters backingOff after rollback completion")

    @pytest.mark.timeout(600)
    def test_backing_off_turn_completes_to_backed_off(self, k8s_clients):
        self._patch_rollback_state(
            k8s_clients,
            {
                "mode": "backingOff",
                "startedAt": "2026-05-25T00:00:00Z",
                "expiryAt": "2000-01-01T00:00:00Z",
                "turn": 1,
                "policyRef": {"kind": "RollbackPolicy", "name": self.ROLLBACK_POLICY_NAME},
                "activeRecommendationFingerprint": "sha256:e2e-backoff-seed",
                "failureReason": "unschedulable",
                "failureMessage": "pod remained Pending with PodScheduled=False Unschedulable",
            },
        )

        def backed_off():
            state = self._rollback_state(k8s_clients)
            if not state or state.get("mode") != "backedOff":
                return False
            annotations = self._deployment(k8s_clients).metadata.annotations or {}
            return (
                "rollback.rightsizing.kubex.ai/requests" not in annotations
                and "rollback.rightsizing.kubex.ai/limits" not in annotations
            )

        wait_for(
            backed_off,
            timeout=180,
            message="rollback backoff turn completed to backedOff",
        )

    @pytest.mark.timeout(600)
    def test_rollback_state_is_cleared_without_matching_policy(self, k8s_clients):
        k8s_clients.custom.delete_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            self.ROLLBACK_POLICY_NAME,
        )

        k8s_clients.apps.patch_namespaced_deployment(
            self.DEPLOYMENT,
            self.NAMESPACE,
            {
                "metadata": {
                    "annotations": {
                        ROLLBACK_STATE_ANNOTATION: json.dumps(
                            {
                                "mode": "idle",
                                "startedAt": "2026-05-25T00:00:00Z",
                                "activeRecommendationFingerprint": "sha256:seed",
                            }
                        )
                    }
                }
            },
        )

        def rollback_state_cleared():
            return self._rollback_state(k8s_clients) is None

        wait_for(
            rollback_state_cleared,
            timeout=180,
            message="rollback-state cleared when no rollback policy matches",
        )

    @pytest.mark.timeout(600)
    def test_expired_backoff_turn_fails_permanently_when_attempts_are_exhausted(self, k8s_clients):
        self._wait_for_pod_resources(k8s_clients, self.INITIAL_RESOURCES, timeout=300)

        k8s_clients.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "rollbackpolicies",
            self.ROLLBACK_POLICY_NAME,
            {
                "spec": {
                    "backoff": {
                        "timePeriod": "1m",
                        "multiplyByTurn": 1,
                        "maxAttempts": 1,
                    }
                }
            },
        )

        self._patch_rollback_state(
            k8s_clients,
            {
                "mode": "backingOff",
                "startedAt": "2026-05-25T00:00:00Z",
                "expiryAt": "2000-01-01T00:00:00Z",
                "turn": 1,
                "policyRef": {"kind": "RollbackPolicy", "name": self.ROLLBACK_POLICY_NAME},
                "activeRecommendationFingerprint": "sha256:e2e-backoff-seed",
                "failureReason": "unschedulable",
                "failureMessage": "pod remained Pending with PodScheduled=False Unschedulable",
            },
        )

        def failed_permanently():
            state = self._rollback_state(k8s_clients)
            if not state:
                return False
            if state.get("mode") != "failedPermanent":
                return False
            annotations = self._deployment(k8s_clients).metadata.annotations or {}
            return (
                "rollback.rightsizing.kubex.ai/requests" not in annotations
                and "rollback.rightsizing.kubex.ai/limits" not in annotations
            )

        wait_for(
            failed_permanently,
            timeout=180,
            message="terminal rollback failure after exhausted backoff attempts",
        )
