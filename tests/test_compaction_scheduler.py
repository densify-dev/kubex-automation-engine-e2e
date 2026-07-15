"""Tests for the Helm-managed dedicated compaction scheduler."""

from __future__ import annotations

import time

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_deployment,
    delete_deployment,
    get_crd,
    get_deployment,
    kubectl,
    wait_for,
    wait_for_crd_condition,
)


class TestCompactionScheduler:
    POLICY_NAMES = [
        "e2e-compaction-blue",
        "e2e-compaction-green",
        "e2e-compaction-drift",
    ]
    DEPLOYMENT_NAMES = [
        "e2e-compaction-blue",
        "e2e-compaction-green",
        "e2e-compaction-drift",
    ]
    NODE_GROUP_LABEL = "kubex.ai/compaction-group"
    NODE_TIER_LABEL = "kubex.ai/compaction-tier"
    NODE_ZONE_LABEL = "kubex.ai/compaction-zone"
    SCHEDULER_NAME = "kubex-compaction-scheduler"
    RUNTIME_NAMESPACE = "kubex"

    @pytest.fixture(autouse=True)
    def cleanup(self, k8s_clients, kube_context, test_namespace):
        yield

        for name in self.POLICY_NAMES:
            self._delete_policy(k8s_clients, name)

        for name in self.DEPLOYMENT_NAMES:
            delete_deployment(k8s_clients.apps, test_namespace, name)

        self._clear_node_labels(kube_context, k8s_clients)

    def _delete_policy(self, k8s_clients, name: str) -> None:
        try:
            k8s_clients.custom.delete_cluster_custom_object(GROUP, VERSION, "clustercompactionpolicies", name)
        except ApiException as exc:
            if exc.status != 404:
                raise
            return

        wait_for(
            lambda: self._policy_missing(k8s_clients, name),
            timeout=30,
            message=f"ClusterCompactionPolicy {name} removal",
        )

    def _policy_missing(self, k8s_clients, name: str) -> bool:
        try:
            k8s_clients.custom.get_cluster_custom_object(GROUP, VERSION, "clustercompactionpolicies", name)
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise
        return False

    def _worker_nodes(self, k8s_clients) -> list[str]:
        workers = sorted(
            node.metadata.name
            for node in k8s_clients.core.list_node().items
            if node.metadata and node.metadata.labels and "worker" in node.metadata.name
        )
        if len(workers) < 2:
            raise RuntimeError("compaction e2e requires at least two Kind worker nodes")
        return workers

    def _label_node(self, kube_context: str, node_name: str, labels: dict[str, str]) -> None:
        args = ["label", "node", node_name]
        for key, value in labels.items():
            args.append(f"{key}={value}")
        args.append("--overwrite")
        kubectl(*args, context=kube_context)

    def _clear_node_labels(self, kube_context: str, k8s_clients) -> None:
        try:
            for node in self._worker_nodes(k8s_clients):
                for key in [self.NODE_GROUP_LABEL, self.NODE_TIER_LABEL, self.NODE_ZONE_LABEL]:
                    kubectl("label", "node", node, f"{key}-", context=kube_context, check=False)
        except Exception:
            # Cleanup should not hide the primary test failure.
            return

    def _policy_manifest(
        self,
        *,
        name: str,
        namespace: str,
        node_selector: dict[str, object],
    ) -> dict:
        return {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "ClusterCompactionPolicy",
            "metadata": {"name": name},
            "spec": {
                "enabled": True,
                "scope": {
                    "workloadTypes": ["Deployment"],
                    "namespaceSelector": {"operator": "In", "values": [namespace]},
                    "labelSelector": {"matchLabels": {"app": name}},
                },
                "scheduler": {"useKubexScheduler": True},
                "descheduler": {
                    "enabled": True,
                    "nodeSelector": node_selector,
                    "maxNoOfPodsToEvictPerNode": 1,
                    "maxNoOfPodsToEvictPerNamespace": 1,
                    "maxNoOfPodsToEvictTotal": 1,
                },
            },
        }

    def _create_policy(self, k8s_clients, namespace: str, name: str, node_selector: dict[str, object]) -> None:
        k8s_clients.custom.create_cluster_custom_object(
            GROUP,
            VERSION,
            "clustercompactionpolicies",
            self._policy_manifest(name=name, namespace=namespace, node_selector=node_selector),
        )

    def _wait_for_policy_ready(self, k8s_clients, name: str) -> dict:
        return wait_for_crd_condition(
            k8s_clients.custom,
            "clustercompactionpolicies",
            name,
            "Available",
            predicate=lambda condition: condition.get("status") == "True",
            timeout=300,
        )

    def _assert_policy_runtime(self, k8s_clients) -> None:
        runtime = get_deployment(k8s_clients.apps, self.RUNTIME_NAMESPACE, self.SCHEDULER_NAME)
        assert runtime.spec.replicas == 1
        assert runtime.spec.template.spec.containers[0].image.startswith("registry.k8s.io/kube-scheduler:")

        configmap = k8s_clients.core.read_namespaced_config_map(f"{self.SCHEDULER_NAME}-config", self.RUNTIME_NAMESPACE)
        rendered = configmap.data["scheduler-config.yaml"]
        assert 'schedulerName: "kubex-compaction-scheduler"' in rendered
        assert "kind: KubeSchedulerConfiguration" in rendered

    def _wait_for_workload_targeting(self, k8s_clients, test_namespace: str, name: str) -> None:
        def deployment_targeted() -> bool:
            deployment = get_deployment(k8s_clients.apps, test_namespace, name)
            template = deployment.spec.template.spec
            return template.scheduler_name == self.SCHEDULER_NAME

        wait_for(deployment_targeted, timeout=300, message=f"deployment {test_namespace}/{name} targeting")

    @pytest.mark.timeout(900)
    def test_workloads_and_runtime_follow_distinct_node_groups(self, k8s_clients, kube_context, test_namespace):
        workers = self._worker_nodes(k8s_clients)
        self._label_node(
            kube_context,
            workers[0],
            {
                self.NODE_GROUP_LABEL: "blue",
                self.NODE_TIER_LABEL: "standard",
                self.NODE_ZONE_LABEL: "north",
            },
        )
        self._label_node(
            kube_context,
            workers[1],
            {
                self.NODE_GROUP_LABEL: "green",
                self.NODE_TIER_LABEL: "spot",
                self.NODE_ZONE_LABEL: "south",
            },
        )

        cases = [
            {
                "policy": "e2e-compaction-blue",
                "node_selector": {self.NODE_GROUP_LABEL: "blue"},
                "workload_selector": {self.NODE_GROUP_LABEL: "blue"},
                "expected_selector": f"nodeSelector: {self.NODE_GROUP_LABEL}=blue",
                "expected_node": workers[0],
            },
            {
                "policy": "e2e-compaction-green",
                "node_selector": {self.NODE_GROUP_LABEL: "green", self.NODE_TIER_LABEL: "spot"},
                "workload_selector": {self.NODE_GROUP_LABEL: "green"},
                "expected_selector": f"nodeSelector: {self.NODE_GROUP_LABEL}=green,{self.NODE_TIER_LABEL}=spot",
                "expected_node": workers[1],
            },
            {
                "policy": "e2e-compaction-drift",
                "node_selector": {
                    "matchExpressions": [
                        {"key": self.NODE_GROUP_LABEL, "operator": "In", "values": ["green"]},
                        {"key": self.NODE_ZONE_LABEL, "operator": "In", "values": ["south"]},
                    ]
                },
                "workload_selector": {self.NODE_ZONE_LABEL: "south"},
                "expected_selector": f"nodeSelector: {self.NODE_GROUP_LABEL} in (green),{self.NODE_ZONE_LABEL} in (south)",
                "expected_node": workers[1],
            },
        ]

        for case in cases:
            self._create_policy(k8s_clients, test_namespace, case["policy"], case["node_selector"])
            create_deployment(
                k8s_clients.apps,
                test_namespace,
                case["policy"],
                node_selector=case["workload_selector"],
            )

            self._wait_for_policy_ready(k8s_clients, case["policy"])
            self._wait_for_workload_targeting(k8s_clients, test_namespace, case["policy"])
            self._assert_policy_runtime(k8s_clients)

            def pod_scheduled_to_expected_node() -> bool:
                pods = k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={case['policy']}").items
                return any(pod.spec and pod.spec.node_name == case["expected_node"] for pod in pods)

            wait_for(pod_scheduled_to_expected_node, timeout=300, message=f"pod {case['policy']} scheduling")

            deployment = get_deployment(k8s_clients.apps, test_namespace, case["policy"])
            assert deployment.spec.template.spec.scheduler_name == self.SCHEDULER_NAME

            policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", case["policy"])
            assert policy["status"]["summary"]["managedWorkloads"] == 1

            delete_deployment(k8s_clients.apps, test_namespace, case["policy"])
            self._delete_policy(k8s_clients, case["policy"])

    @pytest.mark.timeout(900)
    def test_scheduler_image_catches_up_after_drift(self, k8s_clients, kube_context, test_namespace, kube_server_version):
        workers = self._worker_nodes(k8s_clients)
        self._label_node(
            kube_context,
            workers[0],
            {
                self.NODE_GROUP_LABEL: "blue",
                self.NODE_TIER_LABEL: "standard",
                self.NODE_ZONE_LABEL: "north",
            },
        )

        self._create_policy(
            k8s_clients,
            test_namespace,
            "e2e-compaction-drift",
            {self.NODE_GROUP_LABEL: "blue"},
        )
        create_deployment(
            k8s_clients.apps,
            test_namespace,
            "e2e-compaction-drift",
            node_selector={self.NODE_GROUP_LABEL: "blue"},
        )

        self._wait_for_policy_ready(k8s_clients, "e2e-compaction-drift")
        self._wait_for_workload_targeting(k8s_clients, test_namespace, "e2e-compaction-drift")

        expected_tag = f"v{kube_server_version.major}.{kube_server_version.minor.rstrip('+')}.0"
        scheduler_name = self.SCHEDULER_NAME
        config_map_name = f"{scheduler_name}-config"
        deployment = get_deployment(k8s_clients.apps, self.RUNTIME_NAMESPACE, scheduler_name)
        assert deployment.spec.template.spec.containers[0].image.endswith(f":{expected_tag}")

        configmap = k8s_clients.core.read_namespaced_config_map(config_map_name, self.RUNTIME_NAMESPACE)
        rendered = configmap.data["scheduler-config.yaml"]
        assert scheduler_name in rendered
