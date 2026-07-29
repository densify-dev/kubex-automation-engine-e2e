"""Tests: controller_runtime reconcile metrics emission."""

import subprocess
import time

import pytest

from helpers import wait_for


class TestMetrics:
    """Verify controller_runtime reconcile metrics are being emitted."""

    def _get_metrics(
        self,
        kube_context: str,
        controller_namespace: str,
        allow_missing: bool = False,
    ) -> str:
        # Resolve the metrics service name via the k8s API label selector
        svc_name = subprocess.run(
            [
                "kubectl",
                "--context",
                kube_context,
                "get",
                "svc",
                "-n",
                controller_namespace,
                "-l",
                "control-plane=controller-manager",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()

        if not svc_name or "metrics" not in svc_name:
            # Fall back: look for any service with 'metrics' in the name
            all_svcs = (
                subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        kube_context,
                        "get",
                        "svc",
                        "-n",
                        controller_namespace,
                        "-o",
                        "jsonpath={.items[*].metadata.name}",
                    ],
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .split()
            )
            svc_name = next((s for s in all_svcs if "metrics" in s), "")

        if not svc_name:
            if allow_missing:
                pytest.skip("Metrics service not found and metrics are explicitly disabled")
            raise AssertionError("Metrics service not found")

        proc = subprocess.Popen(
            [
                "kubectl",
                "--context",
                kube_context,
                "port-forward",
                f"svc/{svc_name}",
                "18444:8080",
                "-n",
                controller_namespace,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(3)
            # Metrics endpoint is plain HTTP on the service port.
            result = subprocess.run(
                ["curl", "-s", "http://localhost:18444/metrics"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout
        finally:
            proc.terminate()

    def test_reconcile_metrics_present(self, request, kube_context, controller_namespace):
        allow_missing = request.config.getoption("--without-metrics-server")
        # First call establishes whether the metrics service exists at all;
        # skips via pytest.skip() inside _get_metrics when allow_missing and absent.
        metrics = self._get_metrics(kube_context, controller_namespace, allow_missing=allow_missing)
        if "controller_runtime_reconcile_total" in metrics:
            return

        # controller_runtime_reconcile_total is a CounterVec that only appears
        # once at least one reconcile has run for some controller. A preceding
        # test may have just restarted the controller deployment (e.g. to
        # restore env vars), leaving a freshly-started pod with zero reconciles
        # yet - poll rather than assert on a single sample.
        def reconcile_metric_present():
            return "controller_runtime_reconcile_total" in self._get_metrics(
                kube_context, controller_namespace, allow_missing=False
            )

        wait_for(
            reconcile_metric_present,
            timeout=60,
            message="controller_runtime_reconcile_total metric to appear",
        )

    def test_globalconfiguration_reconcile_counted(
        self, request, kube_context, controller_namespace
    ):
        allow_missing = request.config.getoption("--without-metrics-server")
        metrics = self._get_metrics(kube_context, controller_namespace, allow_missing=allow_missing)
        if "globalconfiguration" in metrics.lower():
            return

        def globalconfiguration_metric_present():
            return "globalconfiguration" in self._get_metrics(
                kube_context, controller_namespace, allow_missing=False
            ).lower()

        wait_for(
            globalconfiguration_metric_present,
            timeout=60,
            message="globalconfiguration reconcile metric to appear",
        )
