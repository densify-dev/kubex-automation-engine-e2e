"""Tests: in-cluster Kubex mock receives recommendations and gateway uploads."""

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    automation_strategy_manifest,
    create_multi_container_deployment,
    delete_deployment,
    get_deployment_pod,
    get_crd,
    get_mock_kubex_state,
    proactive_policy_manifest,
    reset_mock_kubex_state,
    wait_for,
)


@pytest.mark.usefixtures("kind_cluster")
class TestKubexMock:
    STRATEGY_NAME = "e2e-kubex-mock-strategy"
    POLICY_NAME = "e2e-kubex-mock-policy"
    DEPLOYMENT = "rightsizing-demo"
    GPU_DEPLOYMENT = "gpu-kubex-demo"

    @pytest.fixture(autouse=True)
    def setup_teardown(self, request, k8s_clients, kube_context, controller_namespace):
        if not request.config.getoption("--deploy-kubex-stub"):
            pytest.skip("kubex mock assertions require --deploy-kubex-stub")

        reset_mock_kubex_state(kube_context, controller_namespace)
        delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)
        delete_deployment(k8s_clients.apps, "default", self.GPU_DEPLOYMENT)
        for plural, name in [
            ("proactivepolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, "default", plural, name
                )
            except ApiException:
                pass

        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        original_spec = dict(gc.get("spec", {}))
        gc["spec"]["heartbeatInterval"] = "1m"
        gc["spec"]["mutationLogInterval"] = "1m"
        gc["spec"]["snapshotInterval"] = "1m"
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "globalconfigurations", "global-config", gc
        )

        def gc_intervals_updated():
            current = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            spec = current.get("spec", {})
            return (
                spec.get("heartbeatInterval") == "1m"
                and spec.get("mutationLogInterval") == "1m"
                and spec.get("snapshotInterval") == "1m"
            )

        wait_for(
            gc_intervals_updated,
            timeout=30,
            message="global configuration interval overrides",
        )

        yield

        delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)
        delete_deployment(k8s_clients.apps, "default", self.GPU_DEPLOYMENT)
        for plural, name in [
            ("proactivepolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, "default", plural, name
                )
            except ApiException:
                pass
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        gc["spec"] = original_spec
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "globalconfigurations", "global-config", gc
        )

    def test_recommendations_are_fetched_from_mock(
        self, k8s_clients, kube_context, controller_namespace
    ):
        def recommendation_fetch_observed():
            state = get_mock_kubex_state(kube_context, controller_namespace)
            return len(state["recommendations"]) > 0

        wait_for(
            recommendation_fetch_observed,
            timeout=60,
            message="recommendation fetch from Kubex mock",
        )

        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        reload_status = gc.get("status", {}).get("recommendationReload", {})
        assert reload_status.get("lastCount", 0) > 0

    @pytest.mark.timeout(300)
    def test_mock_receives_heartbeat_policy_and_mutations(
        self, k8s_clients, kube_context, controller_namespace
    ):
        captured = {"state": None}

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

        def uploads_observed():
            state = get_mock_kubex_state(kube_context, controller_namespace)
            captured["state"] = state
            return (
                len(state["heartbeats"]) > 0
                and len(state["policies"]) > 0
                and len(state["mutations"]) > 0
            )

        wait_for(
            uploads_observed,
            timeout=180,
            message="heartbeat, policy snapshot, and mutation uploads to Kubex mock",
        )

        state = captured["state"]
        assert state is not None
        heartbeat_payload = state["heartbeats"][-1]["payload"]
        assert heartbeat_payload.get("imageTag")
        assert "recommendationReload" in heartbeat_payload

        policy_payload = state["policies"][-1]["payload"]
        assert "policies" in policy_payload
        assert "globalConfiguration" in policy_payload["policies"]

        mutations_payload = state["mutations"][-1]["payload"]
        assert isinstance(mutations_payload, list)
        assert len(mutations_payload) > 0

    @pytest.mark.timeout(300)
    def test_mock_receives_fractional_gpu_mutation(
        self, k8s_clients, kube_context, controller_namespace
    ):
        strategy = automation_strategy_manifest(self.STRATEGY_NAME, "default")
        strategy["spec"]["enablement"]["overrideScheduler"] = "kai"
        strategy["spec"]["experimental"] = {"gpuKaiContract": "v1alpha1-2026-04"}

        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            "default",
            "automationstrategies",
            strategy,
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
            self.GPU_DEPLOYMENT,
            containers=[
                {
                    "name": "app",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )

        def pod_fraction_applied():
            pod = get_deployment_pod(k8s_clients.core, "default", self.GPU_DEPLOYMENT)
            return (pod.metadata.annotations or {}).get("gpu-fraction") == "0.25"

        wait_for(
            pod_fraction_applied,
            timeout=180,
            message="gpu kubex mock pod fractional mutation",
        )

        captured = {"payload": None}

        def gpu_mutation_uploaded():
            state = get_mock_kubex_state(kube_context, controller_namespace)
            for upload in state["mutations"]:
                payload = upload.get("payload") or []
                for mutation in payload:
                    gpu_request = ((mutation.get("mutations") or {}).get("gpu") or {}).get("request")
                    if (
                        mutation.get("entityId") == "entity-gpu-kubex-demo"
                        and mutation.get("containerId") == "container-gpu-kubex-demo"
                        and gpu_request == {"original": 0, "new": 0.25}
                    ):
                        captured["payload"] = mutation
                        return True
            return False

        wait_for(
            gpu_mutation_uploaded,
            timeout=180,
            message="fractional gpu mutation upload to Kubex mock",
        )

        mutation = captured["payload"]
        assert mutation is not None
        assert mutation["policyName"] == f"ProactivePolicy/{self.POLICY_NAME}"
