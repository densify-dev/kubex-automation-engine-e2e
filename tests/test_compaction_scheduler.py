"""Tests for the Helm-managed dedicated compaction scheduler."""


class TestCompactionScheduler:
    def test_compaction_scheduler_image_defaults_to_cluster_version(
        self, k8s_clients, controller_namespace, kube_server_version
    ):
        deployment = k8s_clients.apps.read_namespaced_deployment(
            "kubex-automation-engine-compaction-scheduler", controller_namespace
        )
        container = next(
            c for c in deployment.spec.template.spec.containers if c.name == "kube-scheduler"
        )
        image = container.image
        expected_tag = f"v{kube_server_version.major}.{kube_server_version.minor.rstrip('+')}.0"
        assert image.endswith(f":{expected_tag}")
