"""Tests for the Helm-managed dedicated compaction scheduler."""

from __future__ import annotations

import json
import time

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_deployment,
    create_deployments_bulk,
    create_stateful_set,
    delete_deployment,
    delete_deployments_bulk,
    delete_stateful_set,
    get_crd,
    get_cronjob,
    get_deployment,
    get_stateful_set,
    get_stateful_set_pod,
    kubectl,
    wait_for,
    wait_for_crd_condition,
    wait_for_deployment_ready,
    wait_for_stateful_set_ready,
)


class TestCompactionScheduler:
    POLICY_NAMES = [
        "e2e-compaction-blue",
        "e2e-compaction-green",
        "e2e-compaction-drift",
        "e2e-compaction-move",
        "e2e-compaction-multi",
    ]
    DEPLOYMENT_NAMES = [
        "e2e-compaction-blue",
        "e2e-compaction-green",
        "e2e-compaction-drift",
    ]
    MULTI_WORKLOAD_BASE = "e2e-compaction-multi"
    MULTI_WORKLOAD_COUNT = 10
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
    DESCHEDULER_POLICY_LABEL = "scheduling.kubex.ai/compaction-policy"
    COMPACTION_INTENT_ANNOTATION = "scheduling.kubex.ai/compaction-intent"
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

        delete_deployments_bulk(k8s_clients.apps, test_namespace, self.MULTI_WORKLOAD_BASE, self.MULTI_WORKLOAD_COUNT)

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

    def _worker_nodes(self, k8s_clients) -> list[str]:
        return self._schedulable_nodes(k8s_clients)

    def _runtime_names(self, policy_name: str) -> tuple[str, str]:
        deployment_name = f"{self.DESCHEDULER_PREFIX}-{policy_name}"
        return deployment_name, f"{deployment_name}-config"

    def _find_compaction_descheduler_configmap(self, k8s_clients, policy_name: str):
        selector = (
            f"app.kubernetes.io/component=compaction-descheduler,"
            f"{self.DESCHEDULER_POLICY_LABEL}={policy_name}"
        )
        configmaps = k8s_clients.core.list_namespaced_config_map(
            self.RUNTIME_NAMESPACE,
            label_selector=selector,
        ).items
        if not configmaps:
            raise RuntimeError(f"compaction descheduler configmap for {policy_name} not found")
        return sorted(configmaps, key=lambda cm: cm.metadata.name)[0]

    def _assert_compaction_runtime(self, k8s_clients, policy_name: str, expected_selector: str) -> None:
        cronjob_name, _ = self._runtime_names(policy_name)
        runtime = get_cronjob(k8s_clients.batch, self.RUNTIME_NAMESPACE, cronjob_name)
        pod_spec = runtime.spec.job_template.spec.template.spec
        assert pod_spec.service_account_name == self.DESCHEDULER_PREFIX
        assert pod_spec.containers[0].image.startswith("registry.k8s.io/descheduler/descheduler:")

        configmap = self._find_compaction_descheduler_configmap(k8s_clients, policy_name)
        rendered = configmap.data["policy.yaml"]
        assert "kind: DeschedulerPolicy" in rendered
        assert "- key: scheduling.kubex.ai/compaction-policy" in rendered
        assert "- key: scheduling.kubex.ai/compaction-suppressed" in rendered
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
        workload_types: list[str] | None = None,
        descheduler: dict[str, object] | None = None,
    ) -> dict:
        descheduler_spec = {
            "enabled": True,
            "nodeSelector": node_selector,
            "maxNoOfPodsToEvictPerNode": 1,
            "maxNoOfPodsToEvictPerNamespace": 1,
            "maxNoOfPodsToEvictTotal": 1,
        }
        if descheduler:
            descheduler_spec.update(descheduler)

        return {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "ClusterCompactionPolicy",
            "metadata": {"name": name},
            "spec": {
                "enabled": True,
                "scope": {
                    "workloadTypes": workload_types or ["Deployment"],
                    "namespaceSelector": {"operator": "In", "values": [namespace]},
                    "labelSelector": {"matchLabels": {"app": name}},
                },
                "scheduler": {"useKubexScheduler": True},
                "descheduler": descheduler_spec,
            },
        }

    def _create_policy(
        self,
        k8s_clients,
        namespace: str,
        name: str,
        node_selector: dict[str, object],
        workload_types: list[str] | None = None,
        descheduler: dict[str, object] | None = None,
    ) -> None:
        self._delete_policy(k8s_clients, name)
        k8s_clients.custom.create_cluster_custom_object(
            GROUP,
            VERSION,
            "clustercompactionpolicies",
            self._policy_manifest(
                name=name,
                namespace=namespace,
                node_selector=node_selector,
                workload_types=workload_types,
                descheduler=descheduler,
            ),
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
        def deployment_has_intent() -> bool:
            deployment = get_deployment(k8s_clients.apps, test_namespace, name)
            raw = (deployment.metadata.annotations or {}).get(self.COMPACTION_INTENT_ANNOTATION)
            return bool(raw and json.loads(raw).get("schedulerName") == self.SCHEDULER_NAME)

        wait_for(deployment_has_intent, timeout=300, message=f"deployment {test_namespace}/{name} intent")
        deployment = get_deployment(k8s_clients.apps, test_namespace, name)
        assert deployment.spec.template.spec.scheduler_name in (None, "default-scheduler")
        assert not any(key.startswith("scheduling.kubex.ai/compaction-") for key in (deployment.spec.template.metadata.labels or {}))
        old_pods = k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items

        def replacement_pods_targeted() -> bool:
            pods = k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items
            old_names = {pod.metadata.name for pod in old_pods}
            return pods and all(
                pod.metadata.name not in old_names and pod.spec.scheduler_name == self.SCHEDULER_NAME for pod in pods
            )

        wait_for(replacement_pods_targeted, timeout=300, message=f"deployment {test_namespace}/{name} Pod admission")

    def _wait_for_stateful_set_targeting(self, k8s_clients, test_namespace: str, name: str) -> None:
        def stateful_set_targeted() -> bool:
            stateful_set = get_stateful_set(k8s_clients.apps, test_namespace, name)
            raw = (stateful_set.metadata.annotations or {}).get(self.COMPACTION_INTENT_ANNOTATION)
            return bool(raw and json.loads(raw).get("schedulerName") == self.SCHEDULER_NAME)

        wait_for(stateful_set_targeted, timeout=300, message=f"statefulset {test_namespace}/{name} intent")
        stateful_set = get_stateful_set(k8s_clients.apps, test_namespace, name)
        assert stateful_set.spec.template.spec.scheduler_name in (None, "default-scheduler")
        assert not any(key.startswith("scheduling.kubex.ai/compaction-") for key in (stateful_set.spec.template.metadata.labels or {}))
        old_pod = get_stateful_set_pod(k8s_clients.core, test_namespace, name)

        def replacement_pod_targeted() -> bool:
            pod = get_stateful_set_pod(k8s_clients.core, test_namespace, name)
            return (
                pod.metadata.uid != old_pod.metadata.uid
                and pod.spec.scheduler_name == self.SCHEDULER_NAME
                and pod.metadata.deletion_timestamp is None
            )

        wait_for(replacement_pod_targeted, timeout=300, message=f"statefulset {test_namespace}/{name} Pod admission")

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
                "expected_selector": "e2e-compaction-blue",
                "expected_node": nodes[0],
            },
            {
                "policy": "e2e-compaction-green",
                "node_selector": {
                    "matchExpressions": [
                        {"key": self.NODE_POOL_LABEL, "operator": "In", "values": ["green-a", "green-b", "green-c"]},
                    ]
                },
                "workload_selector": {self.NODE_POOL_LABEL: "green-a"},
                "expected_selector": "e2e-compaction-green",
                "expected_node": nodes[1],
                "workload_types": ["StatefulSet"],
            },
        ]

        for case in cases:
            self._create_policy(
                k8s_clients, test_namespace, case["policy"], case["node_selector"],
                workload_types=case.get("workload_types"),
            )
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
                self._wait_for_stateful_set_targeting(k8s_clients, test_namespace, case["policy"])
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
                assert deployment.spec.template.spec.scheduler_name in (None, "default-scheduler")

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
        workload_labels = {"app": policy_name}
        candidate_affinity = client.V1Affinity(
            node_affinity=client.V1NodeAffinity(
                required_during_scheduling_ignored_during_execution=client.V1NodeSelector(
                    node_selector_terms=[
                        client.V1NodeSelectorTerm(
                            match_expressions=[
                                client.V1NodeSelectorRequirement(
                                    key=self.NODE_POOL_LABEL,
                                    operator="In",
                                    values=["green-a", "green-b", "green-c"],
                                )
                            ]
                        )
                    ]
                ),
            )
        )
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            policy_name,
            service_name=policy_name,
            labels={**workload_labels, "role": "candidate"},
            affinity=candidate_affinity,
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
        seeded_pod = get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name)
        seeded_uid = seeded_pod.metadata.uid
        seeded_node = seeded_pod.spec.node_name
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            "busy-a",
            service_name="busy-a",
            labels={**workload_labels, "role": "busy-a"},
            replicas=8,
            node_selector={self.NODE_POOL_LABEL: "green-b"},
            containers=[
                {
                    "name": "pause",
                    "image": "registry.k8s.io/pause:3.10",
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "100m", "memory": "128Mi"},
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, "busy-a", min_replicas=8)
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            "busy-b",
            service_name="busy-b",
            labels={**workload_labels, "role": "busy-b"},
            replicas=8,
            node_selector={self.NODE_POOL_LABEL: "green-c"},
            containers=[
                {
                    "name": "pause",
                    "image": "registry.k8s.io/pause:3.10",
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "100m", "memory": "128Mi"},
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, "busy-b", min_replicas=8)

        self._create_policy(
            k8s_clients,
            test_namespace,
            policy_name,
            {
                "matchExpressions": [
                    {"key": self.NODE_POOL_LABEL, "operator": "In", "values": ["green-a", "green-b", "green-c"]},
                ]
            },
            workload_types=["StatefulSet"],
            descheduler={
                "defaultEvictor": {
                    "labelSelector": {"matchLabels": {"role": "candidate"}},
                },
                "highNodeUtilization": {
                    # kind nodes have ~1930m allocatable CPU; busy-a/busy-b nodes sit at
                    # ~21% (8×50m/1930m). Use cpu=10 so those nodes are valid destinations
                    # (>10%) while the single-candidate node (~5%) remains a source (<10%).
                    "thresholds": {"cpu": 10, "memory": 10, "pods": 5},
                    "numberOfNodes": 0,
                },
            },
        )

        self._wait_for_policy_ready(k8s_clients, policy_name)
        for name in [policy_name, "busy-a", "busy-b"]:
            self._wait_for_stateful_set_targeting(k8s_clients, test_namespace, name)
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, "busy-a", min_replicas=8)
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, "busy-b", min_replicas=8)
        self._assert_compaction_runtime(
            k8s_clients,
            policy_name,
            f"{self.NODE_POOL_LABEL} in (green-a,green-b,green-c)",
        )

        wait_for(
            lambda: get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name).spec.node_name in set(nodes[:3]),
            timeout=300,
            message=f"candidate {policy_name} initial scheduling",
        )

        def candidate_relocated() -> bool:
            pod = get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name)
            return (
                pod.metadata.uid != seeded_uid
                and pod.spec is not None
                and pod.spec.node_name is not None
                and pod.spec.node_name != seeded_node
                and pod.spec.node_name in set(nodes[:3])
            )

        wait_for(candidate_relocated, timeout=900, message="candidate relocation within the wildcard pool")

        moved_pod = get_stateful_set_pod(k8s_clients.core, test_namespace, policy_name)
        assert moved_pod.spec and moved_pod.spec.node_name in set(nodes[:3])
        assert moved_pod.spec.node_name != seeded_node
        assert moved_pod.metadata.uid != seeded_uid

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

        scheduler_name = self.SCHEDULER_NAME
        config_map_name = f"{scheduler_name}-config"
        deployment = get_deployment(k8s_clients.apps, self.RUNTIME_NAMESPACE, scheduler_name)
        assert deployment.spec.template.spec.containers[0].image.startswith(
            f"registry.k8s.io/kube-scheduler:v{kube_server_version.major}.{kube_server_version.minor.rstrip('+')}."
        )

        configmap = k8s_clients.core.read_namespaced_config_map(config_map_name, self.RUNTIME_NAMESPACE)
        rendered = configmap.data["scheduler-config.yaml"]
        assert scheduler_name in rendered

    @pytest.mark.timeout(300)
    def test_multi_workload_targeting_and_managed_count(self, k8s_clients, test_namespace):
        """Verify the controller correctly targets N Deployments and tracks managedWorkloads.

        Creates MULTI_WORKLOAD_COUNT Deployments with minimal resources and a policy that
        covers all of them via a matchExpressions label selector.  All workloads must
        produce Pods admitted with the compaction schedulerName, and policy.status.summary.managedWorkloads
        must equal the exact count — catching off-by-one bugs and cache-consistency issues
        that only appear when the controller iterates over multiple workloads per reconcile.
        """
        create_deployments_bulk(
            k8s_clients.apps,
            test_namespace,
            self.MULTI_WORKLOAD_BASE,
            self.MULTI_WORKLOAD_COUNT,
            cpu_request="10m",
            mem_request="32Mi",
            cpu_limit="50m",
            mem_limit="64Mi",
        )
        names = [f"{self.MULTI_WORKLOAD_BASE}-{i}" for i in range(self.MULTI_WORKLOAD_COUNT)]

        def all_initial_pods_exist() -> bool:
            return all(
                k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items
                for name in names
            )

        wait_for(all_initial_pods_exist, timeout=240, message=f"all {self.MULTI_WORKLOAD_COUNT} initial Pods")
        initial_pod_uids = {
            pod.metadata.uid
            for name in names
            for pod in k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items
        }

        k8s_clients.custom.create_cluster_custom_object(
            GROUP,
            VERSION,
            "clustercompactionpolicies",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "ClusterCompactionPolicy",
                "metadata": {"name": "e2e-compaction-multi"},
                "spec": {
                    "enabled": True,
                    "scope": {
                        "workloadTypes": ["Deployment"],
                        "namespaceSelector": {"operator": "In", "values": [test_namespace]},
                        # Select only the workloads created by this test; avoids interfering
                        # with other test deployments that share the session namespace.
                        "labelSelector": {
                            "matchExpressions": [
                                {
                                    "key": "app",
                                    "operator": "In",
                                    "values": [
                                        f"{self.MULTI_WORKLOAD_BASE}-{i}"
                                        for i in range(self.MULTI_WORKLOAD_COUNT)
                                    ],
                                }
                            ]
                        },
                    },
                    "scheduler": {"useKubexScheduler": True},
                    "descheduler": {"enabled": False},
                },
            },
        )

        self._wait_for_policy_ready(k8s_clients, "e2e-compaction-multi")

        def all_have_intent() -> bool:
            return all(
                self.COMPACTION_INTENT_ANNOTATION
                in (get_deployment(k8s_clients.apps, test_namespace, name).metadata.annotations or {})
                for name in names
            )

        wait_for(all_have_intent, timeout=240, message=f"all {self.MULTI_WORKLOAD_COUNT} workload intents")
        current_pod_uids = {
            pod.metadata.uid
            for name in names
            for pod in k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items
        }
        assert current_pod_uids == initial_pod_uids, "policy reconciliation must not replace existing Pods"
        old_pod_names = set()
        for name in names:
            deployment = get_deployment(k8s_clients.apps, test_namespace, name)
            assert deployment.spec.template.spec.scheduler_name in (None, "default-scheduler")
            assert not any(
                key.startswith("scheduling.kubex.ai/compaction-")
                for key in (deployment.spec.template.metadata.labels or {})
            )
            for pod in k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items:
                old_pod_names.add(pod.metadata.name)
                k8s_clients.core.delete_namespaced_pod(pod.metadata.name, test_namespace)

        def all_targeted() -> bool:
            for name in names:
                deployment = get_deployment(k8s_clients.apps, test_namespace, name)
                pods = k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items
                if deployment.spec.template.spec.scheduler_name not in (None, "default-scheduler"):
                    return False
                if not pods or not all(
                    pod.metadata.name not in old_pod_names and pod.spec.scheduler_name == self.SCHEDULER_NAME
                    for pod in pods
                ):
                    return False
            return True

        wait_for(all_targeted, timeout=240, message=f"all {self.MULTI_WORKLOAD_COUNT} workloads targeted")

        policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", "e2e-compaction-multi")
        managed = policy["status"]["summary"]["managedWorkloads"]
        assert managed == self.MULTI_WORKLOAD_COUNT, (
            f"expected managedWorkloads={self.MULTI_WORKLOAD_COUNT}, got {managed}"
        )
