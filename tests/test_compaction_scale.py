"""Scale tests for ClusterCompactionPolicy: scheduler targeting throughput and descheduler consolidation.

Run against a real GKE cluster (3x e2-standard-2) via:
    ./test/e2e/run-gke-suite.sh tests/test_compaction_scale.py

Node layout for the consolidation test:
    dense-a  (nodes[0]): 1 filler at 900m CPU → ~49% utilization (above threshold)
    dense-b  (nodes[1]): 1 filler at 900m CPU → ~49% utilization (above threshold)
    sparse   (nodes[2]): 15 candidates at 30m CPU each → ~24% utilization (below threshold)

The descheduler evicts candidate pods from the sparse node onto the dense nodes,
which is the metric we capture (time to drain + post-distribution).
"""

from __future__ import annotations

import os
import time

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_deployment,
    create_deployments_bulk,
    delete_deployment,
    delete_deployments_bulk,
    get_crd,
    get_deployment,
    kubectl,
    wait_for,
    wait_for_crd_condition,
)


class TestCompactionScale:
    SCALE_TIER_LABEL = "scheduling.kubex.ai/compaction-scale-tier"
    WORKLOAD_COUNT = int(os.environ.get("COMPACTION_SCALE_WORKLOAD_COUNT", "100"))
    CANDIDATE_COUNT = 15
    TARGET_BASE = "e2e-scale-target"
    FILLER_BASE = "e2e-scale-filler"
    CANDIDATE_BASE = "e2e-scale-candidate"
    POLICY_NAME = "e2e-compaction-scale"
    SCHEDULER_NAME = "kubex-compaction-scheduler"

    @pytest.fixture(autouse=True)
    def cleanup(self, k8s_clients, kube_context, test_namespace):
        yield
        self._delete_policy(k8s_clients, self.POLICY_NAME)
        delete_deployments_bulk(k8s_clients.apps, test_namespace, self.TARGET_BASE, self.WORKLOAD_COUNT)
        delete_deployments_bulk(k8s_clients.apps, test_namespace, self.FILLER_BASE, 2)
        delete_deployments_bulk(k8s_clients.apps, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT)
        for node in self._schedulable_nodes(k8s_clients):
            kubectl("label", "node", node, f"{self.SCALE_TIER_LABEL}-", context=kube_context, check=False)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _schedulable_nodes(self, k8s_clients) -> list[str]:
        return sorted(
            node.metadata.name
            for node in k8s_clients.core.list_node().items
            if node.metadata
            and node.metadata.name
            and not any(t.effect == "NoSchedule" for t in (node.spec.taints or []))
        )

    def _label_node(self, kube_context: str, node_name: str, node_labels: dict[str, str]) -> None:
        args = ["label", "node", node_name]
        for key, value in node_labels.items():
            args.append(f"{key}={value}")
        args.append("--overwrite")
        kubectl(*args, context=kube_context)

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

    def _wait_for_policy_ready(self, k8s_clients, name: str) -> dict:
        return wait_for_crd_condition(
            k8s_clients.custom,
            "clustercompactionpolicies",
            name,
            "Available",
        )

    def _pods_on_node(self, k8s_clients, namespace: str, base_name: str, count: int, node_name: str) -> int:
        total = 0
        for i in range(count):
            pods = k8s_clients.core.list_namespaced_pod(
                namespace,
                label_selector=f"app={base_name}-{i}",
            ).items
            total += sum(1 for p in pods if p.spec and p.spec.node_name == node_name)
        return total

    # ── tests ─────────────────────────────────────────────────────────────────

    @pytest.mark.timeout(900)
    def test_scheduler_targets_many_workloads(self, k8s_clients, kube_context, test_namespace):
        """Measure controller throughput: time to label WORKLOAD_COUNT Deployments with the compaction scheduler.

        100 Deployments are created with minimal resources (10m CPU, 32Mi RAM each).
        The default scheduler distributes them ~33-34 per node across the 3-node cluster.
        We measure elapsed time from policy creation until all workloads carry the
        kubex-compaction-scheduler schedulerName.
        """
        create_deployments_bulk(
            k8s_clients.apps,
            test_namespace,
            self.TARGET_BASE,
            self.WORKLOAD_COUNT,
            cpu_request="10m",
            mem_request="32Mi",
            cpu_limit="50m",
            mem_limit="64Mi",
        )

        t_start = time.time()

        k8s_clients.custom.create_cluster_custom_object(
            GROUP,
            VERSION,
            "clustercompactionpolicies",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "ClusterCompactionPolicy",
                "metadata": {"name": self.POLICY_NAME},
                "spec": {
                    "enabled": True,
                    "scope": {
                        "workloadTypes": ["Deployment"],
                        "namespaceSelector": {"operator": "In", "values": [test_namespace]},
                    },
                    "scheduler": {"useKubexScheduler": True},
                    "descheduler": {"enabled": False},
                },
            },
        )

        self._wait_for_policy_ready(k8s_clients, self.POLICY_NAME)

        names = [f"{self.TARGET_BASE}-{i}" for i in range(self.WORKLOAD_COUNT)]

        def all_targeted() -> bool:
            for name in names:
                dep = get_deployment(k8s_clients.apps, test_namespace, name)
                if dep.spec.template.spec.scheduler_name != self.SCHEDULER_NAME:
                    return False
            return True

        wait_for(all_targeted, timeout=600, message=f"all {self.WORKLOAD_COUNT} workloads targeted with compaction scheduler")

        elapsed = time.time() - t_start
        throughput = self.WORKLOAD_COUNT / elapsed if elapsed > 0 else float("inf")

        policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", self.POLICY_NAME)
        managed = policy["status"]["summary"]["managedWorkloads"]

        print(f"\n=== Scheduler Targeting Results ===")
        print(f"Workloads:          {self.WORKLOAD_COUNT}")
        print(f"Time to target all: {elapsed:.1f}s  ({throughput:.1f} workloads/s)")
        print(f"managedWorkloads:   {managed}/{self.WORKLOAD_COUNT}")

        assert managed == self.WORKLOAD_COUNT

    @pytest.mark.timeout(1800)
    def test_descheduler_consolidates_sparse_workloads(self, k8s_clients, kube_context, test_namespace):
        """Measure descheduler consolidation: time for sparse-node candidate pods to migrate to dense nodes.

        Node layout (e2-standard-2, ~1850m CPU allocatable):
            dense-a  nodes[0]: 1 filler at 900m CPU → ~49%  (above threshold → valid destination)
            dense-b  nodes[1]: 1 filler at 900m CPU → ~49%  (above threshold → valid destination)
            sparse   nodes[2]: 15 candidates at 30m CPU each → ~24% (below threshold → evict)

        HighNodeUtilization threshold: cpu=30%, targetThresholds.cpu=75%.
        After one descheduler cycle (interval: 2m), all candidate pods should land on dense nodes.
        """
        nodes = self._schedulable_nodes(k8s_clients)
        if len(nodes) < 3:
            pytest.skip(f"requires ≥3 schedulable nodes, got {len(nodes)}")

        dense_a, dense_b, sparse = nodes[0], nodes[1], nodes[2]

        self._label_node(kube_context, dense_a, {self.SCALE_TIER_LABEL: "dense"})
        self._label_node(kube_context, dense_b, {self.SCALE_TIER_LABEL: "dense"})
        self._label_node(kube_context, sparse, {self.SCALE_TIER_LABEL: "sparse"})

        # Filler deployments on dense nodes — one per node, pinned via nodeSelector.
        for i, node_name in enumerate([dense_a, dense_b]):
            create_deployment(
                k8s_clients.apps,
                test_namespace,
                f"{self.FILLER_BASE}-{i}",
                app_label=f"{self.FILLER_BASE}-{i}",
                cpu_request="900m",
                mem_request="512Mi",
                cpu_limit="900m",
                mem_limit="512Mi",
                node_selector={self.SCALE_TIER_LABEL: "dense"},
            )

        # Candidate deployments — no nodeSelector so they can migrate after the compaction
        # scheduler takes over. The default LeastAllocated scheduler places all 15 on the
        # empty sparse node (0% util beats 49% on dense nodes). Once the policy assigns
        # kubex-compaction-scheduler (MostAllocated), replacement pods go to dense nodes.
        create_deployments_bulk(
            k8s_clients.apps,
            test_namespace,
            self.CANDIDATE_BASE,
            self.CANDIDATE_COUNT,
            cpu_request="30m",
            mem_request="64Mi",
            cpu_limit="50m",
            mem_limit="64Mi",
        )

        # Wait for all candidates to land on sparse before creating the policy; this ensures
        # before_on_sparse == CANDIDATE_COUNT and prevents a race where the policy's rolling
        # update starts before pods have even been scheduled.
        def candidates_on_sparse() -> bool:
            return (
                self._pods_on_node(
                    k8s_clients, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT, sparse
                )
                == self.CANDIDATE_COUNT
            )

        wait_for(candidates_on_sparse, timeout=120, message="all candidates scheduled on sparse node")

        t_start = time.time()

        # Scope the policy to candidates only (not fillers). Targeting filler Deployments
        # triggers rolling updates that deadlock: each dense node is full (900m filler), so
        # the new filler pod can't be scheduled until the old one terminates, which doesn't
        # happen until the new one is Running — a scheduling deadlock.
        candidate_app_labels = [f"{self.CANDIDATE_BASE}-{i}" for i in range(self.CANDIDATE_COUNT)]

        k8s_clients.custom.create_cluster_custom_object(
            GROUP,
            VERSION,
            "clustercompactionpolicies",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "ClusterCompactionPolicy",
                "metadata": {"name": self.POLICY_NAME},
                "spec": {
                    "enabled": True,
                    "scope": {
                        "workloadTypes": ["Deployment"],
                        "namespaceSelector": {"operator": "In", "values": [test_namespace]},
                        "labelSelector": {
                            "matchExpressions": [
                                {
                                    "key": "app",
                                    "operator": "In",
                                    "values": candidate_app_labels,
                                }
                            ]
                        },
                    },
                    "scheduler": {"useKubexScheduler": True},
                    "descheduler": {
                        "enabled": True,
                        "interval": "2m",
                        "loopDetectionThreshold": 10,
                        "nodeSelector": {
                            "matchExpressions": [
                                {"key": self.SCALE_TIER_LABEL, "operator": "Exists"},
                            ],
                        },
                        "highNodeUtilization": {
                            "thresholds": {"cpu": 30, "memory": 20},
                            "targetThresholds": {"cpu": 75, "memory": 80},
                        },
                        "maxNoOfPodsToEvictPerNode": self.CANDIDATE_COUNT,
                        "maxNoOfPodsToEvictTotal": self.CANDIDATE_COUNT,
                        "defaultEvictor": {
                            "nodeFit": True,
                            "ignorePvcPods": True,
                        },
                    },
                },
            },
        )

        self._wait_for_policy_ready(k8s_clients, self.POLICY_NAME)

        before_on_sparse = self._pods_on_node(k8s_clients, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT, sparse)

        def sparse_node_drained() -> bool:
            return self._pods_on_node(k8s_clients, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT, sparse) == 0

        wait_for(sparse_node_drained, timeout=1200, message="sparse node drained of candidate pods")

        elapsed = time.time() - t_start
        after_on_sparse = self._pods_on_node(k8s_clients, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT, sparse)
        after_on_dense_a = self._pods_on_node(k8s_clients, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT, dense_a)
        after_on_dense_b = self._pods_on_node(k8s_clients, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT, dense_b)

        print(f"\n=== Descheduler Consolidation Results ===")
        print(f"Candidates:         {self.CANDIDATE_COUNT} Deployments (30m CPU, 64Mi RAM each)")
        print(f"Dense fillers:      2 (900m CPU per node → ~49% utilization)")
        print(f"Threshold:          cpu=30%, targetThresholds.cpu=75%")
        print(f"Before: sparse={before_on_sparse} candidates, dense-a=filler, dense-b=filler")
        print(f"After:  sparse={after_on_sparse}, dense-a={after_on_dense_a} candidates, dense-b={after_on_dense_b} candidates")
        print(f"Time to fully consolidate: {elapsed:.1f}s")

        assert after_on_sparse == 0
