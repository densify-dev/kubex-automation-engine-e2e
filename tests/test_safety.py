"""Tests: safety checks — HPA filter and protected namespace enforcement."""

import time

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    clear_pause_annotations,
    GROUP,
    STATIC_POLICY_ANNOTATION,
    VERSION,
    automation_strategy_manifest,
    create_deployment,
    delete_deployment,
    delete_hpa,
    get_crd,
    get_deployment_pod,
    get_pod_resources,
    static_policy_manifest,
    update_namespace_annotations,
    wait_for,
)


class TestHPAFilter:
    """Verify the controller does not resize pods targeted by an HPA."""

    STRATEGY_NAME = "e2e-hpa-strategy"
    POLICY_NAME = "e2e-hpa-policy"
    DEPLOYMENT = "e2e-hpa-workload"
    HPA_NAME = "e2e-hpa"

    def _cleanup(self, k8s_clients, test_namespace):
        try:
            client.AutoscalingV2Api().delete_namespaced_horizontal_pod_autoscaler(
                self.HPA_NAME, test_namespace
            )
        except ApiException:
            pass
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, test_namespace, plural, name
                )
            except ApiException:
                pass

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        self._cleanup(k8s_clients, test_namespace)

        def stale_strategy_removed():
            try:
                get_crd(
                    k8s_clients.custom,
                    "automationstrategies",
                    self.STRATEGY_NAME,
                    test_namespace,
                )
                return False
            except ApiException as exc:
                if exc.status == 404:
                    return True
                raise

        wait_for(
            stale_strategy_removed,
            timeout=10,
            message="stale HPA test AutomationStrategy removal",
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "automationstrategies",
            automation_strategy_manifest(self.STRATEGY_NAME, test_namespace),
        )
        create_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT, cpu_request="200m")
        hpa = client.V2HorizontalPodAutoscaler(
            metadata=client.V1ObjectMeta(name=self.HPA_NAME, namespace=test_namespace),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    api_version="apps/v1", kind="Deployment", name=self.DEPLOYMENT
                ),
                min_replicas=1,
                max_replicas=3,
                metrics=[
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="cpu",
                            target=client.V2MetricTarget(
                                type="Utilization", average_utilization=80
                            ),
                        ),
                    )
                ],
            ),
        )
        client.AutoscalingV2Api().create_namespaced_horizontal_pod_autoscaler(test_namespace, hpa)
        yield

        self._cleanup(k8s_clients, test_namespace)

    def test_cpu_resize_blocked_when_hpa_present(self, k8s_clients, test_namespace):
        policy = static_policy_manifest(
            self.POLICY_NAME,
            test_namespace,
            strategy_name=self.STRATEGY_NAME,
            label_selector_app=self.DEPLOYMENT,
            cpu_request="50m",  # would be a downsize
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "staticpolicies", policy
        )
        time.sleep(5)
        pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
        resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
        assert resources["app"]["requests"].get("cpu") == "200m", (
            "CPU should not have been resized — HPA is protecting this deployment"
        )

    def test_memory_resize_blocked_when_memory_hpa_present(self, k8s_clients, test_namespace):
        # Replace the CPU-based HPA with a memory-based one.  Use delete_hpa()
        # so we block until the object is fully gone before recreating it with
        # the same name — a bare delete is asynchronous and can cause an
        # AlreadyExists error on the subsequent create.
        delete_hpa(test_namespace, self.HPA_NAME)
        hpa = client.V2HorizontalPodAutoscaler(
            metadata=client.V1ObjectMeta(name=self.HPA_NAME, namespace=test_namespace),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    api_version="apps/v1", kind="Deployment", name=self.DEPLOYMENT
                ),
                min_replicas=1,
                max_replicas=3,
                metrics=[
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="memory",
                            target=client.V2MetricTarget(
                                type="Utilization", average_utilization=80
                            ),
                        ),
                    )
                ],
            ),
        )
        client.AutoscalingV2Api().create_namespaced_horizontal_pod_autoscaler(test_namespace, hpa)
        policy = static_policy_manifest(
            self.POLICY_NAME,
            test_namespace,
            strategy_name=self.STRATEGY_NAME,
            label_selector_app=self.DEPLOYMENT,
            mem_request="32Mi",  # would be a downsize
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "staticpolicies", policy
        )
        time.sleep(5)
        pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
        resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
        assert resources["app"]["requests"].get("memory") == "64Mi", (
            "Memory should not have been resized — memory HPA is protecting this deployment"
        )


class TestProtectedNamespace:
    """Verify the controller refuses to resize workloads in protected namespaces."""

    def test_kube_system_is_protected(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        patterns = gc["spec"].get("protectedNamespacePatterns", [])
        assert any("kube" in p for p in patterns), (
            "kube-* should be in protectedNamespacePatterns by default"
        )

    def test_custom_protected_pattern_persists(self, k8s_clients):
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        original = list(gc["spec"].get("protectedNamespacePatterns", []))
        gc["spec"]["protectedNamespacePatterns"] = original + ["test-protected-*"]
        k8s_clients.custom.replace_cluster_custom_object(
            GROUP, VERSION, "globalconfigurations", "global-config", gc
        )
        try:
            updated = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            assert "test-protected-*" in updated["spec"]["protectedNamespacePatterns"]
        finally:
            for attempt in range(5):
                try:
                    gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
                    gc["spec"]["protectedNamespacePatterns"] = original
                    k8s_clients.custom.replace_cluster_custom_object(
                        GROUP, VERSION, "globalconfigurations", "global-config", gc
                    )
                    break
                except ApiException as e:
                    if e.status != 409 or attempt == 4:
                        raise
                    time.sleep(1)


class TestNamespacePauseUntil:
    """Verify namespace-level pause annotations block and later resume resizing."""

    STRATEGY_NAME = "e2e-namespace-pause-strategy"
    POLICY_NAME = "e2e-namespace-pause-policy"
    DEPLOYMENT = "e2e-namespace-pause-workload"

    def _cleanup(self, k8s_clients, test_namespace):
        delete_deployment(k8s_clients.apps, test_namespace, self.DEPLOYMENT)
        for plural, name in [
            ("staticpolicies", self.POLICY_NAME),
            ("automationstrategies", self.STRATEGY_NAME),
        ]:
            try:
                k8s_clients.custom.delete_namespaced_custom_object(
                    GROUP, VERSION, test_namespace, plural, name
                )
            except ApiException:
                pass

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        self._cleanup(k8s_clients, test_namespace)
        update_namespace_annotations(
            k8s_clients, test_namespace, clear_pause_annotations
        )
        yield
        update_namespace_annotations(
            k8s_clients, test_namespace, clear_pause_annotations
        )
        self._cleanup(k8s_clients, test_namespace)

    @pytest.mark.timeout(900)
    def test_namespace_pause_blocks_then_resumes_resize(self, k8s_clients, test_namespace):
        strategy = automation_strategy_manifest(self.STRATEGY_NAME, test_namespace)
        strategy["spec"]["safetyChecks"] = {
            "enablePauseUntilAnnotationCheck": True,
            "minReadyDuration": "0s",
        }
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            test_namespace,
            "automationstrategies",
            strategy,
        )

        update_namespace_annotations(
            k8s_clients,
            test_namespace,
            lambda annotations: annotations.update(
                {
                    "rightsizing.kubex.ai/pause-until": "infinite",
                    "rightsizing.kubex.ai/pause-reason": "team freeze",
                }
            ),
        )

        create_deployment(
            k8s_clients.apps,
            test_namespace,
            self.DEPLOYMENT,
            cpu_request="100m",
            mem_request="64Mi",
        )
        policy = static_policy_manifest(
            self.POLICY_NAME,
            test_namespace,
            strategy_name=self.STRATEGY_NAME,
            label_selector_app=self.DEPLOYMENT,
            cpu_request="150m",
            mem_request="96Mi",
        )
        k8s_clients.custom.create_namespaced_custom_object(
            GROUP, VERSION, test_namespace, "staticpolicies", policy
        )

        def deployment_has_static_policy_annotation():
            try:
                deployment = k8s_clients.apps.read_namespaced_deployment(
                    self.DEPLOYMENT, test_namespace
                )
                return bool(
                    deployment.metadata.annotations
                    and STATIC_POLICY_ANNOTATION in deployment.metadata.annotations
                )
            except ApiException:
                return False

        wait_for(
            deployment_has_static_policy_annotation,
            timeout=180,
            message="static policy annotation on paused deployment",
        )

        def pause_precheck_failed_for_pod():
            pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
            events = k8s_clients.core.list_namespaced_event(test_namespace).items
            return any(
                event.reason == "PrecheckFailed"
                and "automation paused" in (event.message or "")
                and event.involved_object
                and event.involved_object.kind == "Pod"
                and event.involved_object.name == pod.metadata.name
                and event.involved_object.uid == pod.metadata.uid
                for event in events
            )

        wait_for(
            pause_precheck_failed_for_pod,
            timeout=180,
            message="pause precheck failure event for paused pod",
        )

        pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
        resources = get_pod_resources(k8s_clients.core, test_namespace, pod.metadata.name)
        assert resources["app"]["requests"].get("cpu") == "100m", (
            "CPU request should stay unchanged while the namespace pause is active"
        )
        assert resources["app"]["requests"].get("memory") == "64Mi", (
            "Memory request should stay unchanged while the namespace pause is active"
        )
        annotations = pod.metadata.annotations or {}
        assert "rightsizing.kubex.ai/pause-until" not in annotations, (
            "namespace pause annotations should be evaluated at runtime, not copied to pods"
        )
        assert "rightsizing.kubex.ai/pause-reason" not in annotations, (
            "namespace pause reasons should not be copied to pods"
        )

        update_namespace_annotations(
            k8s_clients, test_namespace, clear_pause_annotations
        )

        def resources_resized():
            live_pod = get_deployment_pod(k8s_clients.core, test_namespace, self.DEPLOYMENT)
            current_resources = get_pod_resources(
                k8s_clients.core, test_namespace, live_pod.metadata.name
            )
            return (
                current_resources["app"]["requests"].get("cpu") == "150m"
                and current_resources["app"]["requests"].get("memory") == "96Mi"
            )

        wait_for(
            resources_resized,
            timeout=450,
            message="resize applied after namespace pause removed",
        )
