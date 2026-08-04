"""Tests: GlobalConfiguration fields and recommendation reload status."""

from helpers import (
    GROUP,
    VERSION,
    automation_strategy_manifest,
    create_multi_container_deployment,
    delete_deployment,
    get_crd,
    get_deployment_pod,
    get_pod_resources,
    proactive_policy_manifest,
    wait_for,
)


class TestGlobalConfiguration:
    """Verify GlobalConfiguration fields are applied and validated."""

    STRATEGY_NAME = "e2e-globalconfig-strategy"
    POLICY_NAME = "e2e-globalconfig-policy"
    DEPLOYMENT = "rightsizing-demo"

    def _cleanup_default_recommendation_policies(self, k8s_clients):
        for plural, name in [
            ("proactivepolicies", "e2e-proactive-policy"),
            ("automationstrategies", "e2e-proactive-strategy"),
            ("proactivepolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, "default", plural, name
                )
            except Exception:
                pass

    def test_protected_namespace_default(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        patterns = gc["spec"].get("protectedNamespacePatterns", [])
        assert any("kube-*" in p or "kube" in p for p in patterns), (
            "Expected kube-* in default protected namespace patterns"
        )

    def test_recommendation_reload_interval_readable(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        # Field may use a cluster default; presence check is sufficient
        assert "recommendationReloadInterval" in gc["spec"]

    def test_automation_globally_disabled_blocks_mutation(
        self, k8s_clients, test_namespace
    ):
        """Disabling automationEnabled globally should block mutation until re-enabled."""
        delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)
        self._cleanup_default_recommendation_policies(k8s_clients)

        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        original = gc["spec"].get("automationEnabled", True)

        gc["spec"]["automationEnabled"] = False
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "globalconfigurations", "global-config", gc
        )
        try:
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                "default",
                "automationstrategies",
                automation_strategy_manifest(self.STRATEGY_NAME, "default"),
            )
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                "default",
                "proactivepolicies",
                proactive_policy_manifest(self.POLICY_NAME, "default", self.STRATEGY_NAME, 365),
            )
            create_multi_container_deployment(
                k8s_clients.apps,
                "default",
                self.DEPLOYMENT,
                containers=[
                    {
                        "name": "demo",
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "200m", "memory": "256Mi"},
                    }
                ],
            )

            import time

            time.sleep(20)
            pod = get_deployment_pod(k8s_clients.core, "default", self.DEPLOYMENT)
            resources = get_pod_resources(k8s_clients.core, "default", pod.metadata.name)
            assert resources["demo"]["requests"].get("cpu") == "100m"
            assert resources["demo"]["requests"].get("memory") == "128Mi"

            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            gc["spec"]["automationEnabled"] = True
            k8s_clients.custom.replace_cluster_custom_object(
                GROUP, VERSION, "globalconfigurations", "global-config", gc
            )

            delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)
            self._cleanup_default_recommendation_policies(k8s_clients)
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                "default",
                "automationstrategies",
                automation_strategy_manifest(self.STRATEGY_NAME, "default"),
            )
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                "default",
                "proactivepolicies",
                proactive_policy_manifest(self.POLICY_NAME, "default", self.STRATEGY_NAME, 365),
            )
            create_multi_container_deployment(
                k8s_clients.apps,
                "default",
                self.DEPLOYMENT,
                containers=[
                    {
                        "name": "demo",
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "200m", "memory": "256Mi"},
                    }
                ],
            )

            def mutation_resumes():
                pod = get_deployment_pod(k8s_clients.core, "default", self.DEPLOYMENT)
                resources = get_pod_resources(k8s_clients.core, "default", pod.metadata.name)
                return (
                    resources["demo"]["requests"].get("cpu") == "250m"
                    and resources["demo"]["requests"].get("memory") == "256Mi"
                )

            wait_for(mutation_resumes, timeout=120, message="global automation re-enabled mutation")
        finally:
            delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)
            self._cleanup_default_recommendation_policies(k8s_clients)
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            gc["spec"]["automationEnabled"] = original
            k8s_clients.custom.replace_cluster_custom_object(
                GROUP, VERSION, "globalconfigurations", "global-config", gc
            )


class TestRecommendations:
    """Verify the controller loads and surfaces recommendation reload status."""

    def test_recommendation_reload_status_updated(self, k8s_clients):
        def has_reload_status():
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            return "recommendationReload" in gc.get("status", {})

        wait_for(
            has_reload_status,
            timeout=120,
            message="GlobalConfiguration recommendation reload status",
        )
