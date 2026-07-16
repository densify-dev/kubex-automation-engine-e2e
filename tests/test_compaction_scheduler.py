"""Tests for the Helm-managed dedicated compaction scheduler."""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_deployment,
    create_stateful_set,
    delete_deployment,
    delete_stateful_set,
    get_crd,
    get_deployment,
    get_stateful_set_pod,
    kubectl,
    wait_for,
    wait_for_crd_condition,
    wait_for_stateful_set_ready,
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
    STATEFULSET_NAMES = [
        "e2e-compaction-green",
        "e2e-compaction-move",
        "busy-a",
        "busy-b",
    ]
    NODE_GROUP_LABEL = "kubex.ai/compaction-group"
    NODE_TIER_LABEL = "kubex.ai/compaction-tier"
    NODE_ZONE_LABEL = "kubex.ai/compaction-zone"
    NODE_POOL_LABEL = "kubex.ai/compaction-group"
    SCHEDULER_NAME = "kubex-compaction-scheduler"
    RUNTIME_NAMESPACE = "kubex"
    DESCHEDULER_PREFIX = "kubex-compaction-descheduler"

    @pytest.fixture(autouse=True)
    def cleanup(self, k8s_clients, kube_context, test_namespace):
        yield

        for name in self.POLICY_NAMES:
            self._delete_policy(k8s_clients, name)

        for name in self.DEPLOYMENT_NAMES:
            delete_deployment(k8s_clients.apps, test_namespace, name)

        for name in self.STATEFULSET_NAMES:
            delete_stateful_set(k8s_clients.apps, k8s_clients.core, test_namespace, name)

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

    def _schedulable_nodes(self, k8s_clients) -> list[str]:
        nodes = sorted(
            node.metadata.name
            for node in k8s_clients.core.list_node().items
            if node.metadata
            and node.metadata.name
            and "control-plane" not in node.metadata.name
        )
        if len(nodes) < 3:
            raise RuntimeError("compaction e2e requires at least three schedulable nodes")
        return nodes

    def _runtime_names(self, policy_name: str) -> tuple[str, str]:
        deployment_name = f"{self.DESCHEDULER_PREFIX}-{policy_name}"
        return deployment_name, f"{deployment_name}-config"

    def _assert_compaction_runtime(self, k8s_clients, policy_name: str, expected_selector: str) -> None:
        deployment_name, configmap_name = self._runtime_names(policy_name)
        runtime = get_deployment(k8s_clients.apps, self.RUNTIME_NAMESPACE, deployment_name)
        assert runtime.spec.replicas == 1
        assert runtime.spec.template.spec.service_account_name == self.DESCHEDULER_PREFIX
        assert runtime.spec.template.spec.containers[0].image.startswith("registry.k8s.io/descheduler/descheduler:")

        configmap = k8s_clients.core.read_namespaced_config_map(configmap_name, self.RUNTIME_NAMESPACE)
        rendered = configmap.data["policy.yaml"]
        assert "kind: DeschedulerPolicy" in rendered
        assert expected_selector in rendered

    def _label_node(self, kube_context: str, node_name: str, labels: dict[str, str]) -> None:
        args = ["label", "node", node_name]
        for key, value in labels.items():
            args.append(f"{key}={value}")
        args.append("--overwrite")
        kubectl(*args, context=kube_context)

    def _clear_node_labels(self, kube_context: str, k8s_clients) -> None:
        try:
            for node in self._schedulable_nodes(k8s_clients):
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
        nodes = self._schedulable_nodes(k8s_clients)
        self._label_node(
            kube_context,
            nodes[0],
            {
                self.NODE_POOL_LABEL: "blue-a",
                self.NODE_TIER_LABEL: "standard",
                self.NODE_ZONE_LABEL: "north",
            },
        )
        self._label_node(
            kube_context,
            nodes[1],
            {
                self.NODE_POOL_LABEL: "green-a",
                self.NODE_TIER_LABEL: "spot",
                self.NODE_ZONE_LABEL: "south",
            },
        )
        self._label_node(
            kube_context,
            nodes[2],
            {
                self.NODE_POOL_LABEL: "green-b",
                self.NODE_TIER_LABEL: "spot",
                self.NODE_ZONE_LABEL: "south",
            },
        )

        cases = [
            {
                "policy": "e2e-compaction-blue",
                "node_selector": {self.NODE_POOL_LABEL: "blue-a"},
                "workload_selector": {self.NODE_POOL_LABEL: "blue-a"},
                "expected_selector": f"{self.NODE_POOL_LABEL}=blue-a",
                "expected_node": nodes[0],
            },
            {
                "policy": "e2e-compaction-green",
                "node_selector": {
                    "matchExpressions": [
                        {"key": self.NODE_POOL_LABEL, "operator": "In", "values": ["green-*"]},
                    ]
                },
                "workload_selector": {self.NODE_POOL_LABEL: "green-a"},
                "expected_selector": f"{self.NODE_POOL_LABEL} in (green-a,green-b)",
                "expected_node": nodes[1],
            },
        ]

        for case in cases:
            self._create_policy(k8s_clients, test_namespace, case["policy"], case["node_selector"])
            if case["policy"] == "e2e-compaction-green":
                create_stateful_set(
                    k8s_clients.apps,
                    test_namespace,
                    case["policy"],
                    service_name=case["policy"],
                    labels={"app": case["policy"]},
                    node_selector=case["workload_selector"],
                    containers=[
                        {
                            "name": "pause",
                            "image": "registry.k8s.io/pause:3.10",
                            "requests": {"cpu": "100m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "128Mi"},
                        }
                    ],
                )
            else:
                create_deployment(
                    k8s_clients.apps,
                    test_namespace,
                    case["policy"],
                    node_selector=case["workload_selector"],
                )

            self._wait_for_policy_ready(k8s_clients, case["policy"])
            if case["policy"] == "e2e-compaction-green":
                wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, case["policy"])
                wait_for(
                    lambda: get_stateful_set_pod(k8s_clients.core, test_namespace, case["policy"]).spec.scheduler_name
                    == self.SCHEDULER_NAME,
                    timeout=300,
                    message=f"statefulset {test_namespace}/{case['policy']} targeting",
                )
            else:
                self._wait_for_workload_targeting(k8s_clients, test_namespace, case["policy"])
            self._assert_compaction_runtime(k8s_clients, case["policy"], case["expected_selector"])

            def pod_scheduled_to_expected_node() -> bool:
                pods = k8s_clients.core.list_namespaced_pod(
                    test_namespace,
                    label_selector=f"app={case['policy']}",
                ).items
                return any(pod.spec and pod.spec.node_name == case["expected_node"] for pod in pods)

            wait_for(pod_scheduled_to_expected_node, timeout=300, message=f"pod {case['policy']} scheduling")

            if case["policy"] == "e2e-compaction-green":
                pod = get_stateful_set_pod(k8s_clients.core, test_namespace, case["policy"])
                assert pod.spec and pod.spec.scheduler_name == self.SCHEDULER_NAME
            else:
                deployment = get_deployment(k8s_clients.apps, test_namespace, case["policy"])
                assert deployment.spec.template.spec.scheduler_name == self.SCHEDULER_NAME

            policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", case["policy"])
            assert policy["status"]["summary"]["managedWorkloads"] == 1

            if case["policy"] == "e2e-compaction-green":
                delete_stateful_set(k8s_clients.apps, k8s_clients.core, test_namespace, case["policy"])
            else:
                delete_deployment(k8s_clients.apps, test_namespace, case["policy"])
            self._delete_policy(k8s_clients, case["policy"])

    @pytest.mark.timeout(1200)
    def test_controller_managed_descheduler_moves_candidate_within_wildcard_pool(
        self, k8s_clients, kube_context, test_namespace
    ):
        nodes = self._schedulable_nodes(k8s_clients)
        for node, pool in zip(nodes[:3], ["green-a", "green-b", "green-c"], strict=True):
            self._label_node(
                kube_context,
                node,
                {
                    self.NODE_POOL_LABEL: pool,
                    self.NODE_TIER_LABEL: "standard",
                    self.NODE_ZONE_LABEL: "south",
                },
            )

        policy_name = "e2e-compaction-move"
        self._create_policy(
            k8s_clients,
            test_namespace,
            policy_name,
            {
                "matchExpressions": [
                    {"key": self.NODE_POOL_LABEL, "operator": "In", "values": ["green-*"]},
                ]
            },
        )

        workload_labels = {"app": policy_name}
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            "busy-a",
            service_name="busy-a",
            labels={**workload_labels, "role": "busy-a"},
            node_selector={self.NODE_POOL_LABEL: "green-b"},
            containers=[
                {
                    "name": "pause",
                    "image": "registry.k8s.io/pause:3.10",
                    "requests": {"cpu": "700m", "memory": "1024Mi"},
                    "limits": {"cpu": "1000m", "memory": "1024Mi"},
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, "busy-a")
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            "busy-b",
            service_name="busy-b",
            labels={**workload_labels, "role": "busy-b"},
            node_selector={self.NODE_POOL_LABEL: "green-c"},
            containers=[
                {
                    "name": "pause",
                    "image": "registry.k8s.io/pause:3.10",
                    "requests": {"cpu": "700m", "memory": "1024Mi"},
                    "limits": {"cpu": "1000m", "memory": "1024Mi"},
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, "busy-b")
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            policy_name,
            service_name=policy_name,
            labels={**workload_labels, "role": "candidate"},
            node_selector={self.NODE_POOL_LABEL: "green-a"},
            containers=[
                {
                    "name": "pause",
                    "image": "registry.k8s.io/pause:3.10",
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, policy_name)

        self._wait_for_policy_ready(k8s_clients, policy_name)
        self._assert_compaction_runtime(
            k8s_clients,
            policy_name,
            f"{self.NODE_POOL_LABEL} in (green-a,green-b,green-c)",
        )

        wait_for(
            lambda: get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name).spec.node_name
            == nodes[0],
            timeout=300,
            message=f"candidate {policy_name} initial scheduling",
        )

        initial_pod = get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name)
        initial_uid = initial_pod.metadata.uid
        initial_node = initial_pod.spec.node_name

        def candidate_moved() -> bool:
            pod = get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name)
            return (
                pod.metadata.uid != initial_uid
                and pod.spec is not None
                and pod.spec.node_name is not None
                and pod.spec.node_name != initial_node
                and pod.spec.node_name in {nodes[1], nodes[2]}
            )

        wait_for(candidate_moved, timeout=900, message="candidate relocation by controller-managed descheduler")

        moved_pod = get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name)
        assert moved_pod.spec and moved_pod.spec.node_name in {nodes[1], nodes[2]}
        assert moved_pod.metadata.uid != initial_uid

        policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", policy_name)
        assert policy["status"]["summary"]["managedWorkloads"] == 3

        for name in ["busy-a", "busy-b", policy_name]:
            delete_stateful_set(k8s_clients.apps, k8s_clients.core, test_namespace, name)
        self._delete_policy(k8s_clients, policy_name)

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
