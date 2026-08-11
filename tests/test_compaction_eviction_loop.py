"""Live eviction-loop suppression coverage for compaction workloads."""

from __future__ import annotations

import json

import pytest
from kubernetes import client

from helpers import create_deployment, delete_deployment, get_deployment, wait_for
from .test_compaction_scheduler import TestCompactionScheduler as _CompactionHelpers

_LOOP_STATE_ANNOTATION = "scheduling.kubex.ai/compaction-state"
_SUPPRESSED_LABEL = "scheduling.kubex.ai/compaction-suppressed"


@pytest.mark.timeout(420)
def test_node_affinity_replacement_loop_is_suppressed(
    k8s_clients, kube_context, test_namespace
):
    """Pods pinned to a node by required affinity trigger suppression after threshold evictions."""
    helper = _CompactionHelpers()
    policy_name = "e2e-compaction-eviction-loop"

    workers = helper._worker_nodes(k8s_clients)
    if not workers:
        pytest.skip("no worker nodes available")

    loop_node = workers[0]
    helper._label_node(
        kube_context,
        loop_node,
        {helper.NODE_GROUP_LABEL: "eviction-loop-test"},
    )

    try:
        helper._create_policy(
            k8s_clients,
            test_namespace,
            policy_name,
            {helper.NODE_GROUP_LABEL: "eviction-loop-test"},
            descheduler={
                "loopDetectionThreshold": 2,
                "loopDetectionWindow": "5m",
                "suppressionDuration": "2m",
                # nodeFit: false — evict even when no alternative node exists for the pod.
                "defaultEvictor": {"nodeFit": False},
            },
        )

        # Required node affinity pins every replacement pod back to the same labeled
        # node, producing the repeated-eviction pattern loop detection is designed to catch.
        node_affinity = client.V1Affinity(
            node_affinity=client.V1NodeAffinity(
                required_during_scheduling_ignored_during_execution=client.V1NodeSelector(
                    node_selector_terms=[
                        client.V1NodeSelectorTerm(
                            match_expressions=[
                                client.V1NodeSelectorRequirement(
                                    key=helper.NODE_GROUP_LABEL,
                                    operator="In",
                                    values=["eviction-loop-test"],
                                )
                            ]
                        )
                    ]
                )
            )
        )
        create_deployment(
            k8s_clients.apps,
            test_namespace,
            policy_name,
            affinity=node_affinity,
        )
        helper._wait_for_policy_ready(k8s_clients, policy_name)
        helper._wait_for_workload_targeting(k8s_clients, test_namespace, policy_name)

        def live_pods():
            return [
                pod
                for pod in k8s_clients.core.list_namespaced_pod(
                    test_namespace, label_selector=f"app={policy_name}"
                ).items
                if pod.metadata.deletion_timestamp is None
            ]

        # Wait for the loop-detection baseline (podNodes map) to be stored, stable,
        # and fully collapsed to exactly one pod.
        #
        # The compaction hook evicts the initial pod before the controller writes the
        # baseline, causing the ReplicaSet to surge-create two replacements at once.
        # Both land on the same node and both get stored in the baseline together.
        # If the test then deletes one of them, the ReplicaSet won't create a new
        # pod (the other already satisfies desired=1), and the surviving pod is
        # already in previousNodes — so loop detection sees no replacement and never
        # increments attempts.  The test's first_replacement_on_same_node check also
        # passes spuriously (the surge sibling is on the same node).
        #
        # We guard by requiring len == 1: only proceed when the surge has fully
        # resolved and a single steady-state pod is captured in the baseline.
        def stable_baseline_captured() -> bool:
            deployment = get_deployment(k8s_clients.apps, test_namespace, policy_name)
            raw = (deployment.metadata.annotations or {}).get(_LOOP_STATE_ANNOTATION, "")
            if not raw:
                return False
            pod_nodes = json.loads(raw).get("podNodes") or {}
            if not pod_nodes:
                return False
            current_uids = {
                str(pod.metadata.uid)
                for pod in live_pods()
                if pod.spec.node_name  # only scheduled pods
            }
            # Baseline must match the live scheduled pods exactly AND there must be
            # exactly one pod. During a ReplicaSet surge two replacements can land
            # simultaneously; if both are in the baseline, deleting one doesn't look
            # like a replacement (the other is already in previousNodes) so loop
            # detection never fires.  Waiting for len == 1 ensures the surge has
            # fully collapsed to a single steady-state pod before we start evicting.
            return set(pod_nodes.keys()) == current_uids and len(current_uids) == 1

        wait_for(stable_baseline_captured, timeout=120, message="stable compaction loop baseline")

        def workload_suppressed() -> bool:
            deployment = get_deployment(k8s_clients.apps, test_namespace, policy_name)
            return (deployment.metadata.labels or {}).get(_SUPPRESSED_LABEL) == "true"

        # First eviction: delete a pod and wait for its replacement to land on the same node.
        pods = live_pods()
        assert pods, "no live pods before first eviction"
        victim1 = pods[0]
        victim1_node = victim1.spec.node_name
        k8s_clients.core.delete_namespaced_pod(victim1.metadata.name, test_namespace)

        def first_replacement_on_same_node() -> bool:
            return any(
                pod.metadata.name != victim1.metadata.name and pod.spec.node_name == victim1_node
                for pod in live_pods()
            )

        wait_for(first_replacement_on_same_node, timeout=120, message="first replacement on evicted node")

        # Wait for the controller to record the first loop event before the second eviction.
        # If we evict again before the reconciler updates the annotation, both replacements
        # would land in the same reconcile cycle, counting as only one loop event.
        def first_loop_event_recorded() -> bool:
            deployment = get_deployment(k8s_clients.apps, test_namespace, policy_name)
            raw = (deployment.metadata.annotations or {}).get(_LOOP_STATE_ANNOTATION, "")
            if not raw:
                return False
            return json.loads(raw).get("attempts", 0) >= 1

        wait_for(first_loop_event_recorded, timeout=60, message="first loop event recorded")

        # Second eviction: same pattern — replacement lands on the same node, threshold reached.
        pods = live_pods()
        assert pods, "no live pods before second eviction"
        victim2 = pods[0]
        victim2_node = victim2.spec.node_name
        k8s_clients.core.delete_namespaced_pod(victim2.metadata.name, test_namespace)

        def second_replacement_on_same_node() -> bool:
            return any(
                pod.metadata.name != victim2.metadata.name and pod.spec.node_name == victim2_node
                for pod in live_pods()
            )

        wait_for(second_replacement_on_same_node, timeout=120, message="second replacement on evicted node")

        wait_for(workload_suppressed, timeout=180, message="eviction-loop suppression")

        current_pods = live_pods()
        assert current_pods, "no live pods after suppression"
        pod_name = current_pods[0].metadata.name
        wait_for(
            lambda: (
                k8s_clients.core.read_namespaced_pod(pod_name, test_namespace).metadata.labels
                or {}
            ).get(_SUPPRESSED_LABEL) == "true",
            timeout=60,
            message="suppression label on current Pod",
        )
    finally:
        delete_deployment(k8s_clients.apps, test_namespace, policy_name)
        helper._delete_policy(k8s_clients, policy_name)
        helper._clear_node_labels(kube_context, k8s_clients)
