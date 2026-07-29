"""Tests: in-cluster Kubex mock receives recommendations and gateway uploads."""

import json
import time
import uuid

import pytest
from kubernetes.client.rest import ApiException

from example_utils import EXAMPLES_ROOT
from helpers import (
    GROUP,
    VERSION,
    automation_strategy_manifest,
    create_multi_container_deployment,
    delete_deployment,
    get_crd,
    get_deployment_pod,
    get_mock_kubex_state,
    get_pod_resources,
    proactive_policy_manifest,
    reset_mock_kubex_state,
    set_mock_kubex_container_id_remap,
    static_policy_manifest,
    wait_for,
)


@pytest.mark.usefixtures("kind_cluster")
class TestKubexMock:
    STRATEGY_NAME = "e2e-kubex-mock-strategy"
    POLICY_NAME = "e2e-kubex-mock-policy"
    DEPLOYMENT = "rightsizing-demo"
    GPU_DEPLOYMENT = "gpu-kubex-demo"
    MULTI_CONTAINER_DEPLOYMENT = "multi-container-demo"

    @staticmethod
    def _wait_for_rollout(apps, namespace, deployment):
        def rolled_out():
            current = apps.read_namespaced_deployment(deployment, namespace)
            desired = current.spec.replicas or 0
            status = current.status
            return (
                status is not None
                and status.observed_generation is not None
                and current.metadata.generation is not None
                and status.observed_generation >= current.metadata.generation
                and status.updated_replicas == desired
                and status.available_replicas == desired
                and status.unavailable_replicas in (None, 0)
            )

        wait_for(rolled_out, timeout=180, message=f"controller deployment {deployment} rollout")

    @pytest.fixture(autouse=True)
    def setup_teardown(self, request, k8s_clients, kube_context, controller_namespace):
        if not request.config.getoption("--deploy-kubex-stub"):
            pytest.skip("kubex mock assertions require --deploy-kubex-stub")

        reset_mock_kubex_state(kube_context, controller_namespace)
        delete_deployment(k8s_clients.apps, "default", self.DEPLOYMENT)
        delete_deployment(k8s_clients.apps, "default", self.GPU_DEPLOYMENT)
        delete_deployment(k8s_clients.apps, "default", self.MULTI_CONTAINER_DEPLOYMENT)
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
        delete_deployment(k8s_clients.apps, "default", self.MULTI_CONTAINER_DEPLOYMENT)
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
    def test_secondary_cluster_mode_fetches_primary_and_passive_clusters(
        self, request, k8s_clients, kube_context, controller_namespace, kind_cluster_name
    ):
        if not request.config.getoption("--secondary-cluster-enabled"):
            pytest.skip("secondary cluster assertions require --secondary-cluster-enabled")
        if not request.config.getoption("--primary-cluster-name"):
            pytest.skip("secondary cluster assertions require --primary-cluster-name")

        recommendations = json.loads((EXAMPLES_ROOT / "recommendations.json").read_text(encoding="utf-8"))
        demo_recommendation = next(
            item
            for item in recommendations["results"]
            if item.get("PodOwnerName") == self.DEPLOYMENT and item.get("Container") == "demo"
        )
        expected_container_id = f"{kind_cluster_name}-{demo_recommendation['ContainerId']}"
        try:
            set_mock_kubex_container_id_remap(kube_context, controller_namespace, True)

            def current_pod():
                return get_deployment_pod(k8s_clients.core, "default", self.DEPLOYMENT)

            def current_resources():
                return get_pod_resources(k8s_clients.core, "default", current_pod().metadata.name)

            def recommendation_applied():
                pod = current_pod()
                resources = current_resources()
                state = get_mock_kubex_state(kube_context, controller_namespace)
                recommendation_clusters = {entry.get("clusterName") for entry in state["recommendations"]}
                state_payloads = [upload.get("payload") or [] for upload in state.get("states", [])]
                remapped_container_ids = {
                    item.get("containerId")
                    for payload in state_payloads
                    for item in payload
                    if isinstance(item, dict)
                }
                return (
                    pod.metadata.deletion_timestamp is None
                    and resources["demo"]["requests"].get("cpu") == "250m"
                    and resources["demo"]["requests"].get("memory") == "256Mi"
                    and resources["demo"]["limits"].get("cpu") == "400m"
                    and resources["demo"]["limits"].get("memory") == "512Mi"
                    and request.config.getoption("--primary-cluster-name") in recommendation_clusters
                    and kind_cluster_name in recommendation_clusters
                    and expected_container_id in remapped_container_ids
                )

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

            wait_for(
                recommendation_applied,
                timeout=300,
                message="secondary/DR recommendation application for rightsizing-demo",
            )
        finally:
            set_mock_kubex_container_id_remap(kube_context, controller_namespace, False)

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
        # GPU requests default to setFromUnspecified: false (unlike CPU/memory, which
        # default to true), so this must be opted in explicitly for the controller to
        # set a fractional GPU request on a pod that doesn't request GPU at all yet.
        strategy["spec"]["enablement"]["gpu"] = {"requests": {"setFromUnspecified": True}}
        strategy["spec"]["experimental"] = {"gpuKaiContract": "v1alpha1-2026-07"}

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

    @pytest.mark.timeout(300)
    def test_mock_receives_automation_state(self, k8s_clients, kube_context, controller_namespace):
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
            self.MULTI_CONTAINER_DEPLOYMENT,
            containers=[
                {
                    "name": "api",
                    "requests": {"cpu": "200m", "memory": "256Mi"},
                    "limits": {"cpu": "400m", "memory": "512Mi"},
                },
                {
                    "name": "worker",
                    "requests": {"cpu": "150m", "memory": "128Mi"},
                    "limits": {"cpu": "300m", "memory": "256Mi"},
                },
            ],
        )

        def automation_state_uploaded():
            state = get_mock_kubex_state(kube_context, controller_namespace)
            captured["state"] = state
            return len(state.get("states", [])) > 0

        wait_for(
            automation_state_uploaded,
            timeout=180,
            message="automation state uploads to Kubex mock",
        )

        state = captured["state"]
        assert state is not None
        states_payload = state["states"][-1]["payload"]
        assert isinstance(states_payload, list)
        api_state = next(
            item for item in states_payload if item.get("containerId") == "container-902"
        )
        assert any(
            reason.get("reasonCode") == "kubex-automation-disabled"
            for reason in api_state["reasons"]
        )

    @pytest.mark.timeout(300)
    def test_automation_blockers_annotation_persists_when_upload_disabled(
        self, k8s_clients, kube_context, controller_namespace, test_namespace
    ):
        suffix = uuid.uuid4().hex[:10]
        strategy_name = f"e2e-state-disabled-strategy-{suffix}"
        policy_name = f"e2e-state-disabled-policy-{suffix}"
        workload_name = f"e2e-state-disabled-workload-{suffix}"
        controller = next(
            deployment
            for deployment in k8s_clients.apps.list_namespaced_deployment(
                controller_namespace, label_selector="control-plane=controller-manager"
            ).items
            if any(
                container.name == "manager"
                for container in deployment.spec.template.spec.containers
            )
        )
        manager = next(
            container
            for container in controller.spec.template.spec.containers
            if container.name == "manager"
        )
        original_env = list(manager.env or [])
        disabled = False

        def patch_controller_env(env):
            k8s_clients.apps.patch_namespaced_deployment(
                controller.metadata.name,
                controller_namespace,
                body={
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{"name": manager.name, "env": env}]
                            }
                        }
                    }
                },
            )

        def env_with_values(env, values):
            result = [item.to_dict() for item in env if item.name not in values]
            result.extend({"name": name, "value": value} for name, value in values.items())
            return result

        try:
            patch_controller_env(
                env_with_values(
                    original_env,
                    {
                        "ENABLE_AUTOMATION_STATE": "false",
                        "AUTOMATION_STATE_INTERVAL": "1s",
                    },
                )
            )
            disabled = True
            self._wait_for_rollout(k8s_clients.apps, controller_namespace, controller.metadata.name)
            reset_mock_kubex_state(kube_context, controller_namespace)

            strategy = automation_strategy_manifest(
                strategy_name,
                test_namespace,
                cpu_downsize=False,
                cpu_upsize=False,
            )
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP, VERSION, test_namespace, "automationstrategies", strategy
            )
            policy = static_policy_manifest(
                policy_name,
                test_namespace,
                strategy_name,
                label_selector_app=workload_name,
                cpu_request="800m",
            )
            policy["spec"]["resources"]["containers"] = {
                "app": {"requests": {"cpu": "800m"}}
            }
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP, VERSION, test_namespace, "staticpolicies", policy
            )
            create_multi_container_deployment(
                k8s_clients.apps,
                test_namespace,
                workload_name,
                containers=[
                    {
                        "name": "app",
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "200m", "memory": "200Mi"},
                    }
                ],
            )

            annotation_key = "rightsizing.kubex.ai/automation-constraints"

            def annotation_observed():
                pod = get_deployment_pod(k8s_clients.core, test_namespace, workload_name)
                raw = (pod.metadata.annotations or {}).get(annotation_key)
                if not raw:
                    return False
                blockers = json.loads(raw)
                assert isinstance(blockers, list), "automation blockers must be a JSON array"
                for blocker in blockers:
                    assert isinstance(blocker, dict), "automation blocker must be a JSON object"
                    if blocker.get("reason") != "automation-strategy-disabled":
                        continue
                    assert "metadata" not in blocker
                    assert "name" not in blocker
                    targets = blocker.get("targets")
                    assert isinstance(targets, list), "automation blocker targets must be an array"
                    for target in targets:
                        assert isinstance(target, dict), "automation blocker target must be an object"
                        assert "metadata" not in target
                        if target == {
                            "container": "app",
                            "usage": "requests",
                            "resource": "cpu",
                        }:
                            return True
                return False

            wait_for(
                annotation_observed,
                timeout=180,
                message="persistent disabled-strategy automation blockers annotation",
            )

            def no_state_upload_for_settle_window():
                # 1s interval makes 5s span multiple potential sender cycles.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = get_mock_kubex_state(kube_context, controller_namespace)
                    if state.get("states") or any(
                        request.get("method") == "POST"
                        and request.get("path", "").endswith("/state")
                        for request in state.get("requests", [])
                    ):
                        return False
                    time.sleep(1)
                return True

            assert no_state_upload_for_settle_window(), (
                "automation state upload occurred while disabled"
            )
        finally:
            try:
                delete_deployment(k8s_clients.apps, test_namespace, workload_name)
                for plural, name in [
                    ("staticpolicies", policy_name),
                    ("automationstrategies", strategy_name),
                ]:
                    try:
                        k8s_clients.custom.delete_namespaced_custom_object(
                            GROUP, VERSION, test_namespace, plural, name
                        )
                    except ApiException:
                        pass
            finally:
                if disabled:
                    patch_controller_env([item.to_dict() for item in original_env])
                    self._wait_for_rollout(
                        k8s_clients.apps, controller_namespace, controller.metadata.name
                    )
