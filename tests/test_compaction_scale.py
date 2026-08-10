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
    wait_for_deployment_ready,
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
    COMPACTION_INTENT_ANNOTATION = "scheduling.kubex.ai/compaction-intent"
    # e2-standard-2 GKE node allocatable CPU in millicores.
    ALLOCATABLE_CPU_M = 1930
    # Target dense-node utilization — well above the 65% HighNodeUtilization threshold.
    DENSE_TARGET_CPU = 0.70

    @pytest.fixture(autouse=True)
    def cleanup(self, k8s_clients, kube_context, test_namespace):
        # Pre-clean any leftovers from previous aborted runs so tests are restartable.
        self._do_cleanup(k8s_clients, kube_context, test_namespace)
        yield
        self._do_cleanup(k8s_clients, kube_context, test_namespace)

    def _do_cleanup(self, k8s_clients, kube_context, test_namespace) -> None:
        self._delete_policy(k8s_clients, self.POLICY_NAME)
        delete_deployments_bulk(k8s_clients.apps, test_namespace, self.TARGET_BASE, self.WORKLOAD_COUNT)
        delete_deployments_bulk(k8s_clients.apps, test_namespace, self.FILLER_BASE, 2)
        delete_deployments_bulk(k8s_clients.apps, test_namespace, self.CANDIDATE_BASE, self.CANDIDATE_COUNT)
        for node in self._schedulable_nodes(k8s_clients):
            kubectl("label", "node", node, f"{self.SCALE_TIER_LABEL}-", context=kube_context, check=False)
        # Wait for all test pods to fully terminate so their CPU/memory requests are released
        # from the nodes. Without this wait, pods from a previous test (e.g. 100 target pods at
        # 10m CPU each, ~333m per node) remain Terminating and counted as "allocated" by the
        # scheduler, filling dense nodes to ~100% and forcing the next test's candidates onto k8pb.
        # Wait only for pods owned by this test to terminate, not all pods in the namespace.
        # Waiting for an empty namespace races with other tests' pods that may still be
        # terminating, causing the scale fixture to time out unnecessarily.
        wait_for(
            lambda: not any(
                (pod.metadata.labels or {}).get("app", "").startswith("e2e-scale-")
                for pod in k8s_clients.core.list_namespaced_pod(test_namespace).items
            ),
            timeout=120,
            message="scale test pods terminated",
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _schedulable_nodes(self, k8s_clients) -> list[str]:
        return sorted(
            node.metadata.name
            for node in k8s_clients.core.list_node().items
            if node.metadata
            and node.metadata.name
            and not any(t.effect == "NoSchedule" for t in (node.spec.taints or []))
        )

    @staticmethod
    def _parse_cpu_millis(cpu_str: str) -> int:
        s = str(cpu_str).strip()
        if s.endswith("m"):
            return int(s[:-1])
        return int(float(s) * 1000)

    def _node_cpu_requests_map(self, k8s_clients) -> dict[str, int]:
        """Return {node_name: total CPU requests in millicores} across all non-terminated pods."""
        schedulable = set(self._schedulable_nodes(k8s_clients))
        cpu_map: dict[str, int] = {n: 0 for n in schedulable}
        for pod in k8s_clients.core.list_pod_for_all_namespaces().items:
            if not pod.spec or not pod.spec.node_name:
                continue
            if pod.spec.node_name not in cpu_map:
                continue
            # Skip terminating pods; their requests are already released.
            if pod.metadata and pod.metadata.deletion_timestamp:
                continue
            for c in pod.spec.containers or []:
                if c.resources and c.resources.requests:
                    raw = c.resources.requests.get("cpu") or "0"
                    cpu_map[pod.spec.node_name] += self._parse_cpu_millis(raw)
        return cpu_map

    def _select_dense_and_sparse_nodes(self, k8s_clients) -> tuple[str, str, str, int, int]:
        """Pick dense_a, dense_b, sparse and per-node filler CPUs (millicores).

        Selects the 2 most-loaded nodes as dense and the least-loaded as sparse so
        the fillers needed to reach the 65% HighNodeUtilization threshold are small
        enough to fit even on heavily-loaded nodes.  Filler sizes are computed per
        node to target DENSE_TARGET_CPU (70%) utilization, capped to leave room for
        rescheduled candidates.
        """
        cpu_map = self._node_cpu_requests_map(k8s_clients)
        nodes = self._schedulable_nodes(k8s_clients)
        sorted_nodes = sorted(nodes, key=lambda n: cpu_map.get(n, 0), reverse=True)
        dense_a, dense_b, sparse = sorted_nodes[0], sorted_nodes[1], sorted_nodes[2]

        target_m = int(self.ALLOCATABLE_CPU_M * self.DENSE_TARGET_CPU)
        # Leave room for rescheduled candidates plus a small margin.
        slack_m = self.CANDIDATE_COUNT * 30 + 50

        def _filler(node: str) -> int:
            base = cpu_map.get(node, 0)
            needed = max(target_m - base, 50)
            cap = self.ALLOCATABLE_CPU_M - base - slack_m
            return min(needed, max(cap, 50))

        return dense_a, dense_b, sparse, _filler(dense_a), _filler(dense_b)

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
        We measure elapsed time from policy creation until all replacement Pods are
        admitted with the kubex-compaction-scheduler schedulerName.
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

        def all_have_intent() -> bool:
            return all(
                self.COMPACTION_INTENT_ANNOTATION
                in (get_deployment(k8s_clients.apps, test_namespace, name).metadata.annotations or {})
                for name in names
            )

        wait_for(all_have_intent, timeout=600, message=f"all {self.WORKLOAD_COUNT} workload intents")
        old_pod_names = set()
        pods_to_delete = []
        for name in names:
            dep = get_deployment(k8s_clients.apps, test_namespace, name)
            # Compaction mutates replacement Pods, not the Deployment template.
            # Kubernetes defaults template.spec.schedulerName to "default-scheduler".
            assert dep.spec.template.spec.scheduler_name in (None, "default-scheduler")
            assert not any(
                key.startswith("scheduling.kubex.ai/compaction-")
                for key in (dep.spec.template.metadata.labels or {})
            )
            for pod in k8s_clients.core.list_namespaced_pod(test_namespace, label_selector=f"app={name}").items:
                old_pod_names.add(pod.metadata.name)
                pods_to_delete.append(pod.metadata.name)

        # Delete in bounded batches to avoid saturating the webhook endpoint.
        _BATCH = 10
        for i in range(0, len(pods_to_delete), _BATCH):
            for pod_name in pods_to_delete[i : i + _BATCH]:
                k8s_clients.core.delete_namespaced_pod(pod_name, test_namespace)
            if i + _BATCH < len(pods_to_delete):
                time.sleep(0.5)

        def all_targeted() -> bool:
            for name in names:
                pods = k8s_clients.core.list_namespaced_pod(
                    test_namespace, label_selector=f"app={name}"
                ).items
                # Only live replacement pods count; skip terminating or pre-deletion pods.
                replacements = [
                    p for p in pods
                    if p.metadata.name not in old_pod_names
                    and p.metadata.deletion_timestamp is None
                ]
                if not replacements:
                    return False
                if not all(
                    p.spec.scheduler_name == self.SCHEDULER_NAME
                    and p.status
                    and p.status.phase == "Running"
                    for p in replacements
                ):
                    return False
            return True

        try:
            wait_for(all_targeted, timeout=600, message=f"all {self.WORKLOAD_COUNT} workloads targeted with compaction scheduler")
        except TimeoutError:
            mismatches = []
            for name in names:
                pods = k8s_clients.core.list_namespaced_pod(
                    test_namespace, label_selector=f"app={name}"
                ).items
                replacements = [
                    p for p in pods
                    if p.metadata.name not in old_pod_names
                    and p.metadata.deletion_timestamp is None
                ]
                if not replacements:
                    mismatches.append(f"{name}: no live replacement pod")
                    continue
                for p in replacements:
                    phase = p.status.phase if p.status else "N/A"
                    sched = p.spec.scheduler_name
                    if sched != self.SCHEDULER_NAME or phase != "Running":
                        mismatches.append(
                            f"{name}: pod={p.metadata.name} scheduler={sched} phase={phase}"
                        )
            if mismatches:
                print(f"\nTargeting mismatches ({len(mismatches)}):\n" + "\n".join(mismatches[:30]))
            raise

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

        Node layout (e2-standard-2, 1930m CPU allocatable):
            dense-a  most-loaded node:  filler sized to reach ~70% CPU  (above threshold → destination)
            dense-b  2nd-most-loaded:   filler sized to reach ~70% CPU  (above threshold → destination)
            sparse   least-loaded node: 15 candidates at 30m CPU each   (below threshold → evict)

        Nodes are selected dynamically by current CPU requests so that filler pods always fit
        even when GKE system pods (KEDA, VPA, etc.) are concentrated on certain nodes.
        Filler sizes are computed per-node to target 70% CPU utilization.

        HighNodeUtilization threshold: cpu=65%, memory=50%, pods=40%. GKE system pods consume
        varying CPU requests per node; sparse node (typically ≤40% CPU incl. candidates) falls
        below all thresholds while dense nodes exceed the CPU threshold. After a few descheduler
        CronJob invocations (interval: */2 * * * *), all candidate pods should land on dense nodes.
        """
        nodes = self._schedulable_nodes(k8s_clients)
        if len(nodes) < 3:
            pytest.skip(f"requires ≥3 schedulable nodes, got {len(nodes)}")

        dense_a, dense_b, sparse, filler_a_cpu, filler_b_cpu = self._select_dense_and_sparse_nodes(k8s_clients)

        self._label_node(kube_context, dense_a, {self.SCALE_TIER_LABEL: "dense"})
        self._label_node(kube_context, dense_b, {self.SCALE_TIER_LABEL: "dense"})
        self._label_node(kube_context, sparse, {self.SCALE_TIER_LABEL: "sparse"})

        # Filler deployments: each dense node gets exactly one filler, pinned to that node via
        # the hostname nodeSelector. The filler CPU is computed per-node to push it to ~70% CPU
        # (above the 65% HighNodeUtilization threshold) regardless of how many system pods the
        # node already carries. Hostname pinning prevents both fillers from landing on the same
        # node when one node cannot accommodate both 900m requests.
        for i, (node_name, filler_cpu) in enumerate([(dense_a, filler_a_cpu), (dense_b, filler_b_cpu)]):
            create_deployment(
                k8s_clients.apps,
                test_namespace,
                f"{self.FILLER_BASE}-{i}",
                app_label=f"{self.FILLER_BASE}-{i}",
                cpu_request=f"{filler_cpu}m",
                mem_request="256Mi",
                cpu_limit=f"{filler_cpu}m",
                mem_limit="256Mi",
                node_selector={"kubernetes.io/hostname": node_name},
            )

        # Wait for fillers to be Running so their CPU requests are visible to the scheduler
        # before candidates are created. Without this, the scheduler may see all nodes as empty
        # and place candidates on dense nodes instead of the sparse node.
        for i in range(2):
            wait_for_deployment_ready(k8s_clients.apps, test_namespace, f"{self.FILLER_BASE}-{i}")

        # Candidate deployments — no nodeSelector so they can migrate after the compaction
        # scheduler takes over. LeastAllocated places most candidates on the sparse node
        # (lowest CPU%). Occasionally 1-2 land on a dense node due to scheduler assumed-cache
        # lag (a dense node's filler may not yet be counted at scheduling time for early pods).
        # The descheduler drains whatever ends up on sparse regardless.
        #
        # Memory request is kept small (8Mi) so that MostAllocated scoring is CPU-dominated.
        # k8pb carries high memory requests from VPA pods (~1834Mi, 30%) which would otherwise
        # nearly equalize the MostAllocated score with the CPU-heavy dense nodes, causing
        # rolling-update replacement pods to land randomly rather than consistently on dense nodes.
        #
        # Zone-level topologySpreadConstraint overrides the kube-scheduler default hostname
        # constraint (maxSkew:3, weight:2). Since all three nodes are in the same GKE zone,
        # every node maps to the same topology domain — PodTopologySpread gives equal scores to
        # all nodes, so NodeResourcesFit (MostAllocated, weight:1) dominates and replacement pods
        # consistently land on the CPU-heavy dense nodes after descheduler eviction.
        create_deployments_bulk(
            k8s_clients.apps,
            test_namespace,
            self.CANDIDATE_BASE,
            self.CANDIDATE_COUNT,
            cpu_request="30m",
            mem_request="8Mi",
            cpu_limit="50m",
            mem_limit="64Mi",
            topology_spread_constraints=[
                client.V1TopologySpreadConstraint(
                    max_skew=100,
                    topology_key="topology.kubernetes.io/zone",
                    when_unsatisfiable="ScheduleAnyway",
                    label_selector=client.V1LabelSelector(match_labels={}),
                )
            ],
        )

        # Wait for all candidate pods to be Running (wherever they landed) before creating
        # the policy. This prevents the policy's rolling update from racing with initial
        # pod scheduling.
        def all_candidates_running() -> bool:
            for i in range(self.CANDIDATE_COUNT):
                pods = k8s_clients.core.list_namespaced_pod(
                    test_namespace,
                    label_selector=f"app={self.CANDIDATE_BASE}-{i}",
                ).items
                if not any(p.status and p.status.phase == "Running" for p in pods):
                    return False
            return True

        wait_for(all_candidates_running, timeout=120, message="all candidates Running")

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
                        "interval": "*/2 * * * *",
                        "loopDetectionThreshold": 10,
                        "nodeSelector": {
                            "matchExpressions": [
                                {"key": self.SCALE_TIER_LABEL, "operator": "Exists"},
                            ],
                        },
                        "highNodeUtilization": {
                            "thresholds": {"cpu": 65, "memory": 50, "pods": 40},
                        },
                        "maxNoOfPodsToEvictPerNode": self.CANDIDATE_COUNT,
                        "maxNoOfPodsToEvictPerNamespace": self.CANDIDATE_COUNT,
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

        dense_a_pct = int((filler_a_cpu) / self.ALLOCATABLE_CPU_M * 100)
        dense_b_pct = int((filler_b_cpu) / self.ALLOCATABLE_CPU_M * 100)

        print(f"\n=== Descheduler Consolidation Results ===")
        print(f"Candidates:         {self.CANDIDATE_COUNT} Deployments (30m CPU, 8Mi RAM each)")
        print(f"Dense-a filler:     {filler_a_cpu}m CPU (+{dense_a_pct}% utilization) on {dense_a}")
        print(f"Dense-b filler:     {filler_b_cpu}m CPU (+{dense_b_pct}% utilization) on {dense_b}")
        print(f"Threshold:          cpu=65%, memory=50%, pods=40% (nodes above threshold are destinations)")
        print(f"Before: sparse={before_on_sparse} candidates, dense-a=filler, dense-b=filler")
        print(f"After:  sparse={after_on_sparse}, dense-a={after_on_dense_a} candidates, dense-b={after_on_dense_b} candidates")
        print(f"Time to fully consolidate: {elapsed:.1f}s")

        assert after_on_sparse == 0
