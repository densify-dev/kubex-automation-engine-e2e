"""Tests: controller pod health, webhook configuration, and metrics endpoint."""

import subprocess
import time

from kubernetes import client

from helpers import get_crd, wait_for


class TestControllerHealth:
    """Verify the controller and its dependencies are healthy."""

    def test_helm_release_metadata_present(self, k8s_clients, controller_namespace, helm_release):
        secrets = k8s_clients.core.list_namespaced_secret(
            controller_namespace,
            label_selector="owner=helm,status=deployed",
        ).items
        assert any(
            secret.metadata.name.startswith(f"sh.helm.release.v1.{helm_release}.")
            for secret in secrets
        ), f"No deployed Helm release secret found for {helm_release}"

    def test_controller_pod_running(self, k8s_clients, controller_namespace):
        pods = k8s_clients.core.list_namespaced_pod(
            controller_namespace,
            label_selector="control-plane=controller-manager",
        ).items
        assert len(pods) >= 1, "No controller pod found"
        for pod in pods:
            assert pod.status.phase == "Running", f"Pod {pod.metadata.name} is {pod.status.phase}"

    def test_all_containers_ready(self, k8s_clients, controller_namespace):
        pods = k8s_clients.core.list_namespaced_pod(
            controller_namespace,
            label_selector="control-plane=controller-manager",
        ).items
        for pod in pods:
            for cs in pod.status.container_statuses or []:
                assert cs.ready, f"Container {cs.name} in pod {pod.metadata.name} is not ready"

    def test_global_configuration_exists(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        assert gc["metadata"]["name"] == "global-config"

    def test_global_configuration_ready(self, k8s_clients):
        def is_ready():
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            conditions = gc.get("status", {}).get("conditions", [])
            if any(
                c["type"] == "PodAdmissionWebhookHealthy" and c["status"] == "True"
                for c in conditions
            ):
                return True
            # Fallback: lastProbeResult field populated (older versions)
            return gc.get("status", {}).get("webhookHealth", {}).get("lastProbeResult") == "Success"

        wait_for(
            is_ready, timeout=120, message="GlobalConfiguration webhook health to be confirmed"
        )

    def test_policy_evaluation_exists(self, k8s_clients):
        pe = get_crd(k8s_clients.custom, "policyevaluations", "policy-evaluation")
        assert pe["metadata"]["name"] == "policy-evaluation"

    def test_mutating_webhook_configured(self, k8s_clients, controller_namespace):
        admissions = client.AdmissionregistrationV1Api()
        webhooks = admissions.list_mutating_webhook_configuration(
            label_selector="control-plane=controller-manager"
        ).items
        assert len(webhooks) >= 1, "No mutating webhook configuration found"
        for wh in webhooks:
            for hook in wh.webhooks or []:
                assert hook.client_config.ca_bundle, f"Webhook {hook.name} has no CA bundle"

    def test_validating_webhook_configured(self, k8s_clients):
        admissions = client.AdmissionregistrationV1Api()
        webhooks = admissions.list_validating_webhook_configuration(
            label_selector="control-plane=controller-manager"
        ).items
        assert len(webhooks) >= 1, "No validating webhook configuration found"

    def test_metrics_endpoint_reachable(self, k8s_clients, controller_namespace, kube_context):
        svc_list = k8s_clients.core.list_namespaced_service(
            controller_namespace, label_selector="control-plane=controller-manager"
        ).items
        metrics_svc = next(
            (s for s in svc_list if s.metadata.name.endswith("-metrics-service")), None
        )
        assert metrics_svc, "Metrics service not found"

        proc = subprocess.Popen(
            [
                "kubectl",
                "--context",
                kube_context,
                "port-forward",
                f"svc/{metrics_svc.metadata.name}",
                "18443:8443",
                "-n",
                controller_namespace,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(3)
            # Metrics endpoint is plain HTTP despite the 8443 port name
            result = subprocess.run(
                ["curl", "-s", "http://localhost:18443/metrics"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, f"Metrics scrape failed: {result.stderr}"
            assert "# HELP" in result.stdout, "Metrics endpoint returned unexpected output"
        finally:
            proc.terminate()
