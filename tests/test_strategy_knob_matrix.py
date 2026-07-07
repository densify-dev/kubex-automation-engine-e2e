"""Tests: broader AutomationStrategy knob matrix."""

import copy
import hashlib
import time
import subprocess

import pytest

from example_utils import (
    EXAMPLES_ROOT,
    manifest_documents,
    skip_reason,
)
from helpers import apply_manifest as apply_manifest_object
from helpers import get_pod_resources, namespace_gone, pod_is_ready, wait_for, wait_for_vpa_recommendation


def _manifest_case_suffix(manifest_path):
    rel_path = manifest_path.relative_to(EXAMPLES_ROOT).with_suffix("").as_posix()
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:8]


def _rewrite_manifest_documents(manifest_path, case_suffix):
    docs = [copy.deepcopy(doc) for doc in manifest_documents(manifest_path)]
    name_map = {}

    for doc in docs:
        kind = doc["kind"]
        metadata = doc["metadata"]
        original_name = metadata["name"]
        if kind in {
            "Namespace",
            "Deployment",
            "AutomationStrategy",
            "ClusterAutomationStrategy",
            "StaticPolicy",
            "ClusterStaticPolicy",
            "VerticalPodAutoscaler",
        }:
            name_map[original_name] = f"{original_name}-{case_suffix}"

    namespace_map = {
        original_name: rewritten_name
        for original_name, rewritten_name in name_map.items()
        if any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == original_name for doc in docs)
    }

    def rewrite_namespace(value):
        return namespace_map.get(value, value)

    for doc in docs:
        kind = doc["kind"]
        metadata = doc["metadata"]
        if metadata["name"] in name_map:
            metadata["name"] = name_map[metadata["name"]]
        if metadata.get("namespace") in namespace_map:
            metadata["namespace"] = namespace_map[metadata["namespace"]]

        spec = doc.get("spec", {})
        scope = spec.get("scope", {})
        selector = scope.get("labelSelector", {}).get("matchLabels", {})
        app_name = selector.get("app")
        if app_name in name_map:
            selector["app"] = name_map[app_name]

        namespace_selector = scope.get("namespaceSelector")
        if namespace_selector and "values" in namespace_selector:
            namespace_selector["values"] = [rewrite_namespace(value) for value in namespace_selector["values"]]

        automation_strategy_ref = spec.get("automationStrategyRef")
        if automation_strategy_ref and automation_strategy_ref.get("name") in name_map:
            automation_strategy_ref["name"] = name_map[automation_strategy_ref["name"]]

        if kind == "Deployment":
            labels = metadata.setdefault("labels", {})
            app_name = labels.get("app")
            if app_name in name_map:
                labels["app"] = name_map[app_name]

            selector_labels = spec.get("selector", {}).get("matchLabels", {})
            app_name = selector_labels.get("app")
            if app_name in name_map:
                selector_labels["app"] = name_map[app_name]

            template_labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
            app_name = template_labels.get("app")
            if app_name in name_map:
                template_labels["app"] = name_map[app_name]

        if kind == "VerticalPodAutoscaler":
            target_ref = spec.get("targetRef", {})
            target_name = target_ref.get("name")
            if target_name in name_map:
                target_ref["name"] = name_map[target_name]

    return docs, namespace_map, name_map


def _apply_manifest_documents(docs, kube_context):
    for doc in docs:
        apply_manifest_object(doc, kube_context)


def _delete_manifest_documents(docs, kube_context, k8s_clients):
    for doc in reversed(docs):
        kind = doc["kind"]
        metadata = doc["metadata"]
        name = metadata["name"]
        namespace = metadata.get("namespace")
        cmd = [
            "kubectl",
            "--context",
            kube_context,
            "delete",
            kind,
            name,
            "--ignore-not-found",
            "--wait=false",
        ]
        if namespace:
            cmd += ["-n", namespace]
        subprocess.run(cmd, capture_output=True)
        if kind == "Namespace":
            wait_for(
                lambda ns=name: namespace_gone(k8s_clients, ns),
                timeout=60,
                message=f"namespace {name} deletion",
            )


def _rewrite_assertions(assertions, namespace_map, name_map):
    rewritten = []
    for namespace, deployment, expected in assertions:
        rewritten.append((namespace_map.get(namespace, namespace), name_map.get(deployment, deployment), expected))
    return rewritten


class TestStrategyKnobMatrix:
    """Verify common strategy knobs keep or change workloads as intended."""

    @pytest.mark.parametrize(
        ("manifest_path", "assertions", "sleep_seconds", "skip_readiness", "pre_warm_manifest_path"),
        [
            (
                EXAMPLES_ROOT / "automationstrategy" / "vpa-filter-default.yaml",
                [
                    # The VPA filter (enableVpaFilter: true) blocks resources that VPA is
                    # *actively managing* — i.e. when the VPA has a live
                    # RecommendationProvided=True status condition.  To make this
                    # deterministic, the test pre-warms the VPA (applying Namespace +
                    # Deployment + VPA without AutomationStrategy first, then waiting for
                    # RecommendationProvided=True) before applying the full manifest.  The
                    # resize is therefore always blocked and the pod retains its original
                    # resource values.
                    ("automationstrategy-vpa-default", "vpa-demo", {"app": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}}),
                ],
                45,
                False,
                EXAMPLES_ROOT / "automationstrategy" / "vpa-filter-prevpa.yaml",
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "limit-range-filter.yaml",
                [
                    # The LimitRange filter blocks per-action: limits cpu 800m and
                    # memory 1024Mi exceed the namespace max (700m / 900Mi) so they are
                    # dropped.  Requests (100m / 128Mi) are within the allowed range and
                    # are applied.  Original limits (600m / 768Mi) are preserved.
                    (
                        "automationstrategy-limitrange",
                        "limitrange-demo",
                        {"app": {"cpu": "100m", "memory": "128Mi", "limits_cpu": "600m", "limits_memory": "768Mi"}},
                    ),
                ],
                20,
                False,
                None,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "pod-limit-range-filter.yaml",
                [
                    (
                        "automationstrategy-podlimitrange",
                        "pod-limitrange-demo",
                        {
                            "app-a": {
                                "cpu": "300m",
                                "memory": "256Mi",
                                "limits_cpu": "500m",
                                "limits_memory": "512Mi",
                            },
                            "app-b": {
                                "cpu": "300m",
                                "memory": "256Mi",
                                "limits_cpu": "500m",
                                "limits_memory": "512Mi",
                            },
                        },
                    ),
                ],
                20,
                False,
                None,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "min-change-thresholds.yaml",
                [
                    (
                        "automationstrategy-minchange",
                        "min-change-demo",
                        {"app": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}},
                    ),
                ],
                20,
                False,
                None,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "min-ready-seconds.yaml",
                [
                    (
                        "automationstrategy-ready",
                        "min-ready-demo",
                        {"app": {"cpu": "300m", "memory": "384Mi", "limits_cpu": "800m", "limits_memory": "768Mi"}},
                    ),
                ],
                20,
                False,
                None,
            ),
            (
                EXAMPLES_ROOT / "automationstrategy" / "node-allocatable-headroom.yaml",
                [
                    (
                        "automationstrategy-node",
                        "node-allocatable-demo",
                        # cpu is intentionally omitted: when the pod is created on a
                        # fresh cluster the webhook may not yet have rightsizing
                        # annotations and the pod schedules with the original 500m;
                        # NodeCapacityFilter then blocks the cpu upsize at runtime
                        # because the node lacks 50% headroom above 8 CPUs.  Either
                        # way, the resize we CAN reliably assert is memory + limits.
                        {"app": {"memory": "16Gi", "limits_cpu": "12", "limits_memory": "24Gi"}},
                    ),
                ],
                # The admission webhook mutates pod resources at creation (before scheduling).
                # On resource-constrained nodes (e.g. CI kind clusters) the pod remains
                # Pending because 8 CPU / 16 Gi cannot be scheduled.  That is the intended
                # behaviour of requireNodeAllocatable — we verify the webhook did its job
                # by asserting the mutated spec values on the Pending pod, so skip the
                # readiness gate.
                20,
                True,
                None,
            ),
            (
                EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster.yaml",
                [
                    (
                        "default",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                    (
                        "example",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                ],
                20,
                False,
                None,
            ),
            (
                EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster-namespace-wins.yaml",
                [
                    (
                        "default",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "256Mi", "limits_cpu": "400m", "limits_memory": "512Mi"}},
                    ),
                    (
                        "example",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                ],
                20,
                False,
                None,
            ),
            (
                EXAMPLES_ROOT / "staticpolicy" / "namespaced-and-cluster-same-weight.yaml",
                [
                    (
                        "default",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                    (
                        "example",
                        "rightsizing-demo",
                        {"demo": {"cpu": "200m", "memory": "296Mi", "limits_cpu": "400m", "limits_memory": "596Mi"}},
                    ),
                ],
                20,
                False,
                None,
            ),
        ],
        ids=[
            "automationstrategy/vpa-filter-default",
            "automationstrategy/limit-range-filter",
            "automationstrategy/pod-limit-range-filter",
            "automationstrategy/min-change-thresholds",
            "automationstrategy/min-ready-seconds",
            "automationstrategy/node-allocatable-headroom",
            "staticpolicy/namespaced-and-cluster",
            "staticpolicy/namespaced-and-cluster-namespace-wins",
            "staticpolicy/namespaced-and-cluster-same-weight",
        ],
    )
    @pytest.mark.timeout(2700)
    def test_strategy_knobs_keep_expected_behavior(
        self,
        manifest_path,
        assertions,
        sleep_seconds,
        skip_readiness,
        pre_warm_manifest_path,
        kube_context,
        k8s_clients,
    ):
        case_suffix = _manifest_case_suffix(manifest_path)
        rewritten_manifest_docs, namespace_map, name_map = _rewrite_manifest_documents(manifest_path, case_suffix)
        rewritten_assertions = _rewrite_assertions(assertions, namespace_map, name_map)
        rewritten_prewarm_docs = (
            _rewrite_manifest_documents(pre_warm_manifest_path, case_suffix)[0]
            if pre_warm_manifest_path is not None
            else None
        )

        reason = skip_reason(manifest_path, kube_context)
        if reason:
            pytest.skip(reason)

        def apply_ordered_manifest_docs(docs):
            deployment_docs = [doc for doc in docs if doc["kind"] == "Deployment"]
            prerequisite_docs = [doc for doc in docs if doc["kind"] != "Deployment"]

            for doc in prerequisite_docs:
                apply_manifest_object(doc, kube_context)

            for doc in prerequisite_docs:
                kind = doc["kind"]
                metadata = doc["metadata"]
                namespace = metadata.get("namespace")
                name = metadata["name"]
                plural = {
                    "AutomationStrategy": "automationstrategies",
                    "StaticPolicy": "staticpolicies",
                }.get(kind)
                if not plural:
                    continue
                wait_for(
                    lambda ns=namespace, pl=plural, nm=name: k8s_clients.custom.get_namespaced_custom_object(
                        "rightsizing.kubex.ai",
                        "v1alpha1",
                        ns,
                        pl,
                        nm,
                    ),
                    timeout=60,
                    message=f"{kind} {namespace}/{name} availability",
                )

            for doc in deployment_docs:
                apply_manifest_object(doc, kube_context)

        try:
            if pre_warm_manifest_path is not None:
                # Phase 1: apply Namespace + Deployment + VPA (no AutomationStrategy).
                # Wait for VPA to produce its first recommendation so that the filter
                # is guaranteed to fire on the very first policyevaluation pass.
                _apply_manifest_documents(rewritten_prewarm_docs, kube_context)
                for namespace, deployment, _ in assertions:
                    # The pre-warm manifest names the VPA after the Deployment.
                    wait_for_vpa_recommendation(
                        kube_context,
                        namespace,
                        deployment,
                        timeout=600,
                    )

            # Phase 2 (or only phase when pre_warm_manifest_path is None): apply
            # the full manifest.  kubectl apply is idempotent so any objects
            # already created by the pre-warm step are simply confirmed.
            if manifest_path in {
                EXAMPLES_ROOT / "automationstrategy" / "min-ready-seconds.yaml",
                EXAMPLES_ROOT / "automationstrategy" / "node-allocatable-headroom.yaml",
            }:
                apply_ordered_manifest_docs(rewritten_manifest_docs)
            else:
                _apply_manifest_documents(rewritten_manifest_docs, kube_context)

            def current_pod(namespace: str, deployment: str, expected=None):
                pods = k8s_clients.core.list_namespaced_pod(
                    namespace, label_selector=f"app={deployment}"
                ).items
                if expected is not None:
                    matching_pods = []
                    for pod in pods:
                        if pod.metadata.deletion_timestamp is not None:
                            continue
                        resources = get_pod_resources(k8s_clients.core, namespace, pod.metadata.name)
                        if all(
                            (
                                values.get("cpu") is None
                                or resources[container]["requests"].get("cpu") == values["cpu"]
                            )
                            and (
                                values.get("memory") is None
                                or resources[container]["requests"].get("memory") == values["memory"]
                            )
                            and (
                                values.get("limits_cpu") is None
                                or resources[container]["limits"].get("cpu") == values["limits_cpu"]
                            )
                            and (
                                values.get("limits_memory") is None
                                or resources[container]["limits"].get("memory") == values["limits_memory"]
                            )
                            for container, values in expected.items()
                        ):
                            matching_pods.append(pod)
                    if matching_pods:
                        return sorted(matching_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]
                    raise RuntimeError(
                        f"no pod found for deployment {namespace}/{deployment} with expected resources"
                    )

                ready_pods = [
                    p for p in pods if p.metadata.deletion_timestamp is None and pod_is_ready(p)
                ]
                if not ready_pods:
                    raise RuntimeError(f"no ready pod found for deployment {namespace}/{deployment}")

                return sorted(ready_pods, key=lambda pod: pod.metadata.creation_timestamp)[-1]

            time.sleep(5)
            if not skip_readiness:
                for namespace, deployment, _ in rewritten_assertions:
                    wait_for(
                        lambda ns=namespace, dep=deployment: pod_is_ready(current_pod(ns, dep)),
                        timeout=180,
                        message=f"workload readiness for {namespace}/{deployment}",
                    )

            time.sleep(sleep_seconds)

            for namespace, deployment, expected in rewritten_assertions:
                wait_for(
                    lambda ns=namespace, dep=deployment, exp=expected: current_pod(ns, dep, exp),
                    timeout=600,
                    message=f"expected resources for {namespace}/{deployment}",
                )
                pod = current_pod(namespace, deployment, expected)
                resources = get_pod_resources(k8s_clients.core, namespace, pod.metadata.name)
                for container, values in expected.items():
                    container_resources = resources[container]
                    if values.get("cpu") is not None:
                        assert container_resources["requests"].get("cpu") == values["cpu"]
                    if values.get("memory") is not None:
                        assert container_resources["requests"].get("memory") == values["memory"]
                    if values.get("limits_cpu") is not None:
                        assert container_resources["limits"].get("cpu") == values["limits_cpu"]
                    if values.get("limits_memory") is not None:
                        assert container_resources["limits"].get("memory") == values["limits_memory"]
        finally:
            if rewritten_manifest_docs is not None:
                _delete_manifest_documents(rewritten_manifest_docs, kube_context, k8s_clients)
            if rewritten_prewarm_docs is not None:
                _delete_manifest_documents(rewritten_prewarm_docs, kube_context, k8s_clients)
