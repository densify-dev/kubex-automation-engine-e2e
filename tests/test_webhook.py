"""Tests: mutating webhook annotation injection and health probing."""

import json
import time

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    clear_pause_annotations,
    GROUP,
    RIGHTSIZING_ANNOTATION,
    VERSION,
    automation_strategy_manifest,
    create_multi_container_deployment,
    delete_deployment,
    get_deployment_pod,
    get_crd,
    get_pod_resources,
    pod_is_ready,
    static_policy_manifest,
    update_namespace_annotations,
    wait_for,
)


class TestWebhookAnnotations:
    """Verify the mutating webhook injects rightsizing annotations into new pods."""

    DEPLOYMENT = "e2e-webhook-workload"
    STRATEGY_NAME = "e2e-webhook-strategy"
    POLICY_NAME = "e2e-webhook-policy"
    NAMESPACE = "default"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients):
        delete_deployment(k8s_clients.apps, self.NAMESPACE, "rightsizing-demo")
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, plural, name
                )
            except ApiException:
                pass
        time.sleep(1)
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "automationstrategies",
            automation_strategy_manifest(self.STRATEGY_NAME, self.NAMESPACE),
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.NAMESPACE,
            "staticpolicies",
            static_policy_manifest(
                self.POLICY_NAME,
                self.NAMESPACE,
                self.STRATEGY_NAME,
                label_selector_app="rightsizing-demo",
                cpu_request="250m",
                mem_request="256Mi",
            ),
        )
        create_multi_container_deployment(
            k8s_clients.apps,
            self.NAMESPACE,
            "rightsizing-demo",
            containers=[
                {
                    "name": "demo",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )
        yield
        delete_deployment(k8s_clients.apps, self.NAMESPACE, "rightsizing-demo")
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, self.NAMESPACE, plural, name
                )
            except ApiException:
                pass

    def test_webhook_probe_annotation_handled(self, k8s_clients):
        """GlobalConfiguration status should reflect webhook health probing and pod annotation injection."""

        def webhook_healthy():
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            conditions = gc.get("status", {}).get("conditions", [])
            return any(
                c["type"] == "PodAdmissionWebhookHealthy" and c["status"] == "True"
                for c in conditions
            )

        wait_for(webhook_healthy, timeout=120, message="PodAdmissionWebhookHealthy condition")

        def pod_mutated_by_webhook():
            pod = get_deployment_pod(k8s_clients.core, self.NAMESPACE, "rightsizing-demo")
            resources = get_pod_resources(k8s_clients.core, self.NAMESPACE, pod.metadata.name)
            annotation = (pod.metadata.annotations or {}).get(RIGHTSIZING_ANNOTATION, "")
            return pod, resources, annotation

        captured = {"pod": None, "resources": None, "annotation": ""}

        def webhook_resize_present():
            pod, resources, annotation = pod_mutated_by_webhook()
            app = resources.get("demo", {})
            requests = app.get("requests", {})
            limits = app.get("limits", {})
            if not (
                pod_is_ready(pod)
                and requests.get("cpu") == "250m"
                and requests.get("memory") == "256Mi"
                and limits.get("cpu") == "400m"
                and limits.get("memory") == "512Mi"
            ):
                return False
            captured["pod"] = pod
            captured["resources"] = resources
            captured["annotation"] = annotation
            return True

        wait_for(webhook_resize_present, timeout=120, message="webhook pod mutation")
        assert captured["pod"] is not None
        if captured["annotation"]:
            assert captured["annotation"].strip()
        json.dumps(captured["resources"])


class TestWebhookProbeConfiguration:
    """Verify GlobalConfiguration webhookProbe customizations are accepted by live probing."""

    def test_webhook_probe_custom_image_metadata_and_resources(self, k8s_clients):
        original = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        original_spec = dict(original.get("spec", {}))
        original_probe_time = (
            original.get("status", {}).get("webhookHealth", {}).get("lastProbeTime")
        )

        updated = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        updated["spec"]["webhookHealth"] = {
            "failureThreshold": 1,
            "successThreshold": 1,
            "transitionCheckInterval": "1s",
        }
        updated["spec"]["webhookProbe"] = {
            "image": "registry.k8s.io/pause:3.10",
            "labels": {"e2e.kubex.ai/probe-label": "configured"},
            "annotations": {"e2e.kubex.ai/probe-annotation": "configured"},
            "resources": {
                "requests": {"cpu": "5m", "memory": "16Mi"},
                "limits": {"cpu": "10m", "memory": "32Mi"},
            },
        }
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "globalconfigurations", "global-config", updated
        )
        try:
            def customized_probe_succeeded():
                gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
                status = gc.get("status", {})
                webhook_health = status.get("webhookHealth", {})
                conditions = status.get("conditions", [])
                return (
                    webhook_health.get("lastProbeResult") == "Success"
                    and webhook_health.get("lastProbeTime") != original_probe_time
                    and any(
                        c["type"] == "PodAdmissionWebhookHealthy" and c["status"] == "True"
                        for c in conditions
                    )
                )

            wait_for(
                customized_probe_succeeded,
                timeout=180,
                message="customized webhook probe success",
            )
        finally:
            for attempt in range(5):
                try:
                    gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
                    gc["spec"] = original_spec
                    k8s_clients.custom.replace_cluster_custom_object(
                        GROUP, VERSION, "globalconfigurations", "global-config", gc
                    )
                    break
                except ApiException as exc:
                    if exc.status != 409 or attempt == 4:
                        raise
                    time.sleep(1)


class TestWebhookNamespacePause:
    """Verify namespace pause annotations block admission-time mutation."""

    DEPLOYMENT = "e2e-webhook-paused-workload"
    STRATEGY_NAME = "e2e-webhook-paused-strategy"
    POLICY_NAME = "e2e-webhook-paused-policy"

    def _cleanup(self, k8s_clients, test_namespace):
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

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        self._cleanup(k8s_clients, test_namespace)
        update_namespace_annotations(
            k8s_clients, test_namespace, clear_pause_annotations
        )
        yield
        update_namespace_annotations(
            k8s_clients, test_namespace, clear_pause_annotations
        )
        self._cleanup(k8s_clients, test_namespace)

    @pytest.mark.timeout(900)
    def test_namespace_pause_skips_admission_mutation(self, k8s_clients, test_namespace):
        def webhook_healthy():
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            conditions = gc.get("status", {}).get("conditions", [])
            return any(
                c["type"] == "PodAdmissionWebhookHealthy" and c["status"] == "True"
                for c in conditions
            )

        wait_for(webhook_healthy, timeout=120, message="PodAdmissionWebhookHealthy condition")

        strategy = automation_strategy_manifest(self.STRATEGY_NAME, test_namespace)
        strategy["spec"]["safetyChecks"] = {
            "enablePauseUntilAnnotationCheck": True,
        }
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "automationstrategies",
            strategy,
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "staticpolicies",
            static_policy_manifest(
                self.POLICY_NAME,
                test_namespace,
                self.STRATEGY_NAME,
                label_selector_app=self.DEPLOYMENT,
                cpu_request="250m",
                mem_request="256Mi",
            ),
        )

        update_namespace_annotations(
            k8s_clients,
            test_namespace,
            lambda annotations: annotations.update(
                {
                    "rightsizing.kubex.ai/pause-until": "infinite",
                    "rightsizing.kubex.ai/pause-reason": "team freeze",
                }
            ),
        )

        create_multi_container_deployment(
            k8s_clients.apps,
            test_namespace,
            self.DEPLOYMENT,
            containers=[
                {
                    "name": "demo",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )

        def paused_pod_ready_without_mutation():
            pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
            resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
            annotation = (pod.metadata.annotations or {}).get(RIGHTSIZING_ANNOTATION, "")
            return (
                pod_is_ready(pod)
                and resources["demo"]["requests"].get("cpu") == "100m"
                and resources["demo"]["requests"].get("memory") == "128Mi"
                and resources["demo"]["limits"].get("cpu") == "200m"
                and resources["demo"]["limits"].get("memory") == "256Mi"
                and annotation == ""
            )

        wait_for(
            paused_pod_ready_without_mutation,
            timeout=180,
            message="paused namespace pod admission without mutation",
        )

        paused_pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
        paused_annotations = paused_pod.metadata.annotations or {}
        assert "rightsizing.kubex.ai/pause-until" not in paused_annotations, (
            "namespace pause annotations should not be copied onto admitted pods"
        )
        assert "rightsizing.kubex.ai/pause-reason" not in paused_annotations, (
            "namespace pause reasons should not be copied onto admitted pods"
        )

        update_namespace_annotations(
            k8s_clients, test_namespace, clear_pause_annotations
        )
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        create_multi_container_deployment(
            k8s_clients.apps,
            test_namespace,
            self.DEPLOYMENT,
            containers=[
                {
                    "name": "demo",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )

        def unpaused_pod_mutated():
            pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
            resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
            annotation = (pod.metadata.annotations or {}).get(RIGHTSIZING_ANNOTATION, "")
            return (
                pod_is_ready(pod)
                and resources["demo"]["requests"].get("cpu") == "250m"
                and resources["demo"]["requests"].get("memory") == "256Mi"
                and resources["demo"]["limits"].get("cpu") == "400m"
                and resources["demo"]["limits"].get("memory") == "512Mi"
                and annotation.strip() != ""
            )

        wait_for(
            unpaused_pod_mutated,
            timeout=180,
            message="unpaused namespace pod admission with mutation",
        )
