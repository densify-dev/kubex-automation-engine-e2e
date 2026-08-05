"""Live eviction-loop suppression coverage for compaction workloads."""

from __future__ import annotations

import copy

import pytest
from kubernetes import client

from helpers import create_deployment, delete_deployment, get_deployment, wait_for
from .test_compaction_scheduler import TestCompactionScheduler as _CompactionHelpers


@pytest.mark.timeout(420)
def test_webhook_failure_replacement_loop_is_suppressed(
    k8s_clients, kube_context, test_namespace
):
    """Repeated replacements that bypass admission trigger workload suppression."""
    helper = _CompactionHelpers()
    policy_name = "e2e-compaction-eviction-loop"
    admission = client.AdmissionregistrationV1Api()
    webhook_configs = admission.list_mutating_webhook_configuration().items
    kubex_configs = [
        config
        for config in webhook_configs
        if any("kubex" in webhook.name and "pod" in webhook.name for webhook in config.webhooks or [])
    ]
    if not kubex_configs:
        pytest.skip("Kubex Pod mutating webhook configuration is not installed")

    workers = helper._worker_nodes(k8s_clients)
    helper._label_node(
        kube_context,
        workers[0],
        {helper.NODE_GROUP_LABEL: "eviction-loop-test"},
    )
    original_configs = [copy.deepcopy(config) for config in kubex_configs]

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
            },
        )
        create_deployment(
            k8s_clients.apps,
            test_namespace,
            policy_name,
            node_selector={helper.NODE_GROUP_LABEL: "eviction-loop-test"},
        )
        helper._wait_for_policy_ready(k8s_clients, policy_name)
        helper._wait_for_workload_targeting(k8s_clients, test_namespace, policy_name)

        # Break the webhook service reference. failurePolicy=Ignore lets replacement
        # Pods start with the default scheduler while admission is unavailable.
        for config in kubex_configs:
            for webhook in config.webhooks:
                service = webhook.client_config.service
                service.name = f"{service.name}-unavailable"
            admission.replace_mutating_webhook_configuration(config.metadata.name, config)

        pods = k8s_clients.core.list_namespaced_pod(
            test_namespace, label_selector=f"app={policy_name}"
        ).items
        assert pods
        k8s_clients.core.delete_namespaced_pod(pods[0].metadata.name, test_namespace)

        def replacement_bypassed_webhook() -> bool:
            current = k8s_clients.core.list_namespaced_pod(
                test_namespace, label_selector=f"app={policy_name}"
            ).items
            return any(
                pod.metadata.name != pods[0].metadata.name
                and pod.metadata.deletion_timestamp is None
                and pod.spec.scheduler_name != helper.SCHEDULER_NAME
                for pod in current
            )

        wait_for(replacement_bypassed_webhook, timeout=180, message="unmutated replacement Pod")

        # The compaction controller may have already evicted the first replacement via
        # setLabelsByEviction; pick the first live (non-terminating) pod that bypassed
        # the webhook so that the subsequent delete call does not 404.
        first_replacement = next(
            pod
            for pod in k8s_clients.core.list_namespaced_pod(
                test_namespace, label_selector=f"app={policy_name}"
            ).items
            if pod.metadata.name != pods[0].metadata.name
            and pod.metadata.deletion_timestamp is None
        )
        k8s_clients.core.delete_namespaced_pod(first_replacement.metadata.name, test_namespace)
        wait_for(
            lambda: any(
                pod.metadata.name != first_replacement.metadata.name
                and pod.metadata.deletion_timestamp is None
                and pod.spec.scheduler_name != helper.SCHEDULER_NAME
                for pod in k8s_clients.core.list_namespaced_pod(
                    test_namespace, label_selector=f"app={policy_name}"
                ).items
            ),
            timeout=180,
            message="second unmutated replacement Pod",
        )

        def workload_suppressed() -> bool:
            deployment = get_deployment(k8s_clients.apps, test_namespace, policy_name)
            return (
                deployment.metadata.labels or {}
            ).get("scheduling.kubex.ai/compaction-suppressed") == "true"

        wait_for(workload_suppressed, timeout=180, message="eviction-loop suppression")

        replacement = next(
            pod
            for pod in k8s_clients.core.list_namespaced_pod(
                test_namespace, label_selector=f"app={policy_name}"
            ).items
            if pod.metadata.deletion_timestamp is None
        )
        wait_for(
            lambda: (
                k8s_clients.core.read_namespaced_pod(
                    replacement.metadata.name, test_namespace
                ).metadata.labels
                or {}
            ).get("scheduling.kubex.ai/compaction-suppressed")
            == "true",
            timeout=60,
            message="suppression label on current Pod",
        )
    finally:
        for config in original_configs:
            admission.replace_mutating_webhook_configuration(config.metadata.name, config)
        delete_deployment(k8s_clients.apps, test_namespace, policy_name)
        helper._delete_policy(k8s_clients, policy_name)
        helper._clear_node_labels(kube_context, k8s_clients)
