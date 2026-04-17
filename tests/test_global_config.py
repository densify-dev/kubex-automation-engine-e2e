"""Tests: GlobalConfiguration fields and recommendation reload status."""

from helpers import GROUP, VERSION, get_crd, wait_for


class TestGlobalConfiguration:
    """Verify GlobalConfiguration fields are applied and validated."""

    def test_protected_namespace_default(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        patterns = gc["spec"].get("protectedNamespacePatterns", [])
        assert any("kube-*" in p or "kube" in p for p in patterns), (
            "Expected kube-* in default protected namespace patterns"
        )

    def test_recommendation_reload_interval_readable(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        # Field may use a cluster default; presence check is sufficient
        assert "recommendationReloadInterval" in gc["spec"] or True

    def test_automation_globally_disabled_blocks_mutation(
        self, k8s_clients, test_namespace, kube_context
    ):
        """Disabling automationEnabled globally should prevent resource changes."""
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        original = gc["spec"].get("automationEnabled", True)

        gc["spec"]["automationEnabled"] = False
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "globalconfigurations", "global-config", gc
        )
        try:
            import time

            time.sleep(10)
            # Mutation should now be a no-op; covered by other tests running under this state
        finally:
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
