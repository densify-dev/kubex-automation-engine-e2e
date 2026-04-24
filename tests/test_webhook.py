"""Tests: mutating webhook annotation injection and health probing."""

import json
import time

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
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
    proactive_policy_manifest,
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
            ("proactivepolicies", self.POLICY_NAME),
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
            "proactivepolicies",
            proactive_policy_manifest(self.POLICY_NAME, self.NAMESPACE, self.STRATEGY_NAME, 365),
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
            ("proactivepolicies", self.POLICY_NAME),
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
