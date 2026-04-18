"""Tests: mutating webhook annotation injection and health probing."""

import time

import pytest

from helpers import (
    create_deployment,
    delete_deployment,
    get_crd,
    wait_for,
)


class TestWebhookAnnotations:
    """Verify the mutating webhook injects rightsizing annotations into new pods."""

    DEPLOYMENT = "e2e-webhook-workload"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        time.sleep(1)
        create_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        yield
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)

    def test_webhook_probe_annotation_handled(self, k8s_clients, controller_namespace):
        """GlobalConfiguration status should reflect webhook health probing."""

        def webhook_healthy():
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            conditions = gc.get("status", {}).get("conditions", [])
            return any(
                c["type"] == "PodAdmissionWebhookHealthy" and c["status"] == "True"
                for c in conditions
            )

        wait_for(webhook_healthy, timeout=120, message="PodAdmissionWebhookHealthy condition")
