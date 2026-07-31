"""Tests: controller_runtime reconcile metrics emission."""

import subprocess

import pytest

from helpers import _get_free_local_port, port_forward_service, wait_for


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

        # Use the shared port-forward helper (ephemeral local port, waits for
        # the tunnel to actually accept connections) rather than a fixed
        # port - test_* methods below poll this repeatedly, and a shared
        # fixed port would race both the OS releasing it between calls and
        # any other concurrent port-forward on the same CI host.
        local_port = _get_free_local_port()
        with port_forward_service(kube_context, controller_namespace, svc_name, local_port, 8080):
            # Metrics endpoint is plain HTTP on the service port.
            result = subprocess.run(
                ["curl", "-s", f"http://localhost:{local_port}/metrics"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout

    def _wait_for_metric(
        self, request, kube_context, controller_namespace, substring, message, case_insensitive=False
    ):
        allow_missing = request.config.getoption("--without-metrics-server")
        # First call establishes whether the metrics service exists at all;
        # skips via pytest.skip() inside _get_metrics when allow_missing and absent.
        metrics = self._get_metrics(kube_context, controller_namespace, allow_missing=allow_missing)
        needle = substring.lower() if case_insensitive else substring

        def metric_present(text):
            return needle in (text.lower() if case_insensitive else text)

        if metric_present(metrics):
            return

        # Some reconcile-derived metrics only appear once a controller has
        # reconciled at least once. A preceding test may have just restarted
        # the controller deployment (e.g. to restore env vars), leaving a
        # freshly-started pod with zero reconciles yet - poll rather than
        # assert on a single sample.
        def condition():
            return metric_present(
                self._get_metrics(kube_context, controller_namespace, allow_missing=False)
            )

        wait_for(condition, timeout=60, message=message)

    def test_reconcile_metrics_present(self, request, kube_context, controller_namespace):
        self._wait_for_metric(
            request,
            kube_context,
            controller_namespace,
            "controller_runtime_reconcile_total",
            "controller_runtime_reconcile_total metric to appear",
        )

    def test_globalconfiguration_reconcile_counted(
        self, request, kube_context, controller_namespace
    ):
        self._wait_for_metric(
            request,
            kube_context,
            controller_namespace,
            "globalconfiguration",
            "globalconfiguration reconcile metric to appear",
            case_insensitive=True,
        )
