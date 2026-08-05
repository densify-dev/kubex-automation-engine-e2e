"""E2E coverage for ClusterCompactionPolicy with setLabelsByEviction: false.

When setLabelsByEviction is disabled the controller patches pod metadata
directly instead of evicting pods and relying on webhook re-admission.
Existing pods must receive compaction labels and the compaction-intent
annotation WITHOUT being replaced, and their schedulerName must not change.
"""

from __future__ import annotations

import json

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_deployment,
    delete_deployment,
    get_deployment,
    wait_for,
    wait_for_crd_condition,
)

COMPACTION_POLICY_LABEL = "scheduling.kubex.ai/compaction-policy"
COMPACTION_SCHEDULER_NAME_LABEL = "scheduling.kubex.ai/compaction-scheduler-name"
COMPACTION_INTENT_ANNOTATION = "scheduling.kubex.ai/compaction-intent"
RUNTIME_HOOK_ANNOTATION = "clustercompactionpolicy.rightsizing.kubex.ai/pod-runtime-hook"
KUBEX_SCHEDULER = "kubex-compaction-scheduler"


def _create_direct_patch_policy(k8s_clients, namespace: str, name: str) -> None:
    """Create a ClusterCompactionPolicy with setLabelsByEviction: false."""
    try:
        k8s_clients.custom.delete_cluster_custom_object(
            GROUP, VERSION, "clustercompactionpolicies", name
        )
        wait_for(
            lambda: _policy_gone(k8s_clients, name),
            timeout=30,
            message=f"ClusterCompactionPolicy {name} removal",
        )
    except ApiException as exc:
        if exc.status != 404:
            raise

    k8s_clients.custom.create_cluster_custom_object(
        GROUP,
        VERSION,
        "clustercompactionpolicies",
        {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "ClusterCompactionPolicy",
            "metadata": {"name": name},
            "spec": {
                "enabled": True,
                "setLabelsByEviction": False,
                "scope": {
                    "workloadTypes": ["Deployment"],
                    "namespaceSelector": {"operator": "In", "values": [namespace]},
                    "labelSelector": {"matchLabels": {"app": name}},
                },
                "scheduler": {"useKubexScheduler": True},
                "descheduler": {"enabled": True},
            },
        },
    )


def _delete_policy(k8s_clients, name: str) -> None:
    try:
        k8s_clients.custom.delete_cluster_custom_object(
            GROUP, VERSION, "clustercompactionpolicies", name
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
        return
    wait_for(
        lambda: _policy_gone(k8s_clients, name),
        timeout=30,
        message=f"ClusterCompactionPolicy {name} removal",
    )


def _policy_gone(k8s_clients, name: str) -> bool:
    try:
        k8s_clients.custom.get_cluster_custom_object(
            GROUP, VERSION, "clustercompactionpolicies", name
        )
    except ApiException as exc:
        if exc.status == 404:
            return True
        raise
    return False


@pytest.mark.timeout(300)
def test_direct_patch_labels_existing_pods_without_eviction(k8s_clients, test_namespace):
    """setLabelsByEviction=false patches existing pods in place; no replacement occurs."""
    policy_name = "e2e-compaction-direct-patch"

    try:
        create_deployment(k8s_clients.apps, test_namespace, policy_name)
        _create_direct_patch_policy(k8s_clients, test_namespace, policy_name)

        wait_for_crd_condition(
            k8s_clients.custom,
            "clustercompactionpolicies",
            policy_name,
            "Available",
            predicate=lambda c: c.get("status") == "True",
            timeout=120,
        )

        # Capture the initial pod set — we expect these SAME pods to be patched, not replaced.
        initial_pods = k8s_clients.core.list_namespaced_pod(
            test_namespace, label_selector=f"app={policy_name}"
        ).items
        assert initial_pods, "deployment must have at least one pod before the policy takes effect"
        initial_uids = {pod.metadata.uid for pod in initial_pods}

        def pods_have_compaction_labels() -> bool:
            pods = k8s_clients.core.list_namespaced_pod(
                test_namespace, label_selector=f"app={policy_name}"
            ).items
            live = [p for p in pods if p.metadata.deletion_timestamp is None]
            return bool(live) and all(
                p.metadata.labels
                and p.metadata.labels.get(COMPACTION_POLICY_LABEL) == policy_name
                and COMPACTION_INTENT_ANNOTATION in (p.metadata.annotations or {})
                for p in live
            )

        wait_for(pods_have_compaction_labels, timeout=120, message="compaction labels patched onto existing pods")

        pods = k8s_clients.core.list_namespaced_pod(
            test_namespace, label_selector=f"app={policy_name}"
        ).items
        live_pods = [p for p in pods if p.metadata.deletion_timestamp is None]
        assert live_pods, "expected at least one live pod after patching"

        for pod in live_pods:
            labels = pod.metadata.labels or {}
            annotations = pod.metadata.annotations or {}

            # Compaction intent labels must be applied.
            assert labels.get(COMPACTION_POLICY_LABEL) == policy_name, (
                f"pod {pod.metadata.name}: missing {COMPACTION_POLICY_LABEL}={policy_name}"
            )
            assert labels.get(COMPACTION_SCHEDULER_NAME_LABEL) == KUBEX_SCHEDULER, (
                f"pod {pod.metadata.name}: missing {COMPACTION_SCHEDULER_NAME_LABEL}={KUBEX_SCHEDULER}"
            )

            # compaction-intent annotation must be well-formed.
            raw_intent = annotations.get(COMPACTION_INTENT_ANNOTATION)
            assert raw_intent, f"pod {pod.metadata.name}: missing {COMPACTION_INTENT_ANNOTATION}"
            intent = json.loads(raw_intent)
            assert intent.get("policyName") == policy_name, (
                f"pod {pod.metadata.name}: intent.policyName = {intent.get('policyName')!r}, want {policy_name!r}"
            )
            assert intent.get("schedulerName") == KUBEX_SCHEDULER, (
                f"pod {pod.metadata.name}: intent.schedulerName = {intent.get('schedulerName')!r}, "
                f"want {KUBEX_SCHEDULER!r}"
            )

            # pod-runtime-hook must NOT be set — it is only written in eviction mode.
            assert RUNTIME_HOOK_ANNOTATION not in annotations, (
                f"pod {pod.metadata.name}: unexpected {RUNTIME_HOOK_ANNOTATION} in direct-patch mode"
            )

            # pod.spec.schedulerName must not be changed — it is immutable and is only
            # updated through an evict-and-readmit cycle, which does not happen here.
            assert pod.spec.scheduler_name != KUBEX_SCHEDULER, (
                f"pod {pod.metadata.name}: schedulerName was changed to {KUBEX_SCHEDULER!r}; "
                "no eviction should occur in direct-patch mode"
            )

        # The pods must be the SAME pods — their UIDs must match the initial set,
        # confirming no eviction replaced them.
        current_uids = {pod.metadata.uid for pod in live_pods}
        replaced = current_uids - initial_uids
        assert not replaced, (
            f"unexpected new pod UIDs after direct-patch: {replaced} "
            "(pods were evicted/replaced in direct-patch mode — should not happen)"
        )

        # Workload object must have compaction intent stamped on it too.
        deployment = get_deployment(k8s_clients.apps, test_namespace, policy_name)
        raw = (deployment.metadata.annotations or {}).get(COMPACTION_INTENT_ANNOTATION)
        assert raw, "deployment missing compaction-intent annotation"
        workload_intent = json.loads(raw)
        assert workload_intent.get("schedulerName") == KUBEX_SCHEDULER

        # Pod-template must NOT receive compaction labels — only the workload owner and
        # existing pods are patched; the template is left intact so new pods get
        # admission-webhook treatment on the next rollout.
        assert not any(
            key.startswith("scheduling.kubex.ai/compaction-")
            for key in (deployment.spec.template.metadata.labels or {})
        ), "pod-template labels must not be modified in direct-patch mode"

    finally:
        delete_deployment(k8s_clients.apps, test_namespace, policy_name)
        _delete_policy(k8s_clients, policy_name)
