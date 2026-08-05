"""GKE cluster k8s version upgrade test for ClusterCompactionPolicy.

Upgrades a live GKE cluster (control plane then node pool) and verifies that
ClusterCompactionPolicy state is fully preserved: custom resources survive the
upgrade, workload labels remain intact, and the controller re-establishes targeting
after its pod restarts on the new node version — without any manual intervention.

Required env vars:
    GKE_CLUSTER_NAME              GKE cluster name (e.g. compaction-e2e)
    GKE_ZONE                      GKE zone (e.g. us-central1-b)
    GKE_PROJECT_ID                GCP project
    COMPACTION_UPGRADE_K8S_VERSION  Target k8s minor version (e.g. "1.35")
                                  or full GKE version (e.g. "1.35.3-gke.1234567")

Optional env vars:
    GKE_NODE_POOL        Node pool to upgrade (default: default-pool)
    CONTROLLER_NAMESPACE Namespace where the controller runs (default: kubex)
    CONTROLLER_DEPLOYMENT Controller Deployment name (default: kubex-automation-engine)

Run via:
    COMPACTION_UPGRADE_K8S_VERSION=1.35 ./test/e2e/run-gke-suite.sh tests/test_compaction_upgrade.py
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_deployments_bulk,
    delete_deployments_bulk,
    get_crd,
    get_deployment,
    wait_for,
)


class TestCompactionClusterUpgrade:
    WORKLOAD_BASE = "e2e-upgrade"
    WORKLOAD_COUNT = 5
    POLICY_NAME = "e2e-compaction-upgrade"
    SCHEDULER_NAME = "kubex-compaction-scheduler"
    COMPACTION_INTENT_ANNOTATION = "scheduling.kubex.ai/compaction-intent"

    CONTROLLER_NAMESPACE = os.environ.get("CONTROLLER_NAMESPACE", "kubex")
    CONTROLLER_DEPLOYMENT = os.environ.get("CONTROLLER_DEPLOYMENT", "kubex-automation-engine")
    GKE_CLUSTER_NAME = os.environ.get("GKE_CLUSTER_NAME", "")
    GKE_ZONE = os.environ.get("GKE_ZONE", "")
    GKE_PROJECT_ID = os.environ.get("GKE_PROJECT_ID", "")
    GKE_NODE_POOL = os.environ.get("GKE_NODE_POOL", "default-pool")
    UPGRADE_K8S_VERSION = os.environ.get("COMPACTION_UPGRADE_K8S_VERSION", "")

    # ── fixtures ──────────────────────────────────────────────────────────────

    @pytest.fixture(autouse=True)
    def cleanup(self, k8s_clients, test_namespace):
        yield
        self._delete_policy(k8s_clients)
        delete_deployments_bulk(
            k8s_clients.apps, test_namespace, self.WORKLOAD_BASE, self.WORKLOAD_COUNT
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _require_gke(self) -> None:
        missing = [
            name
            for name, val in [
                ("GKE_CLUSTER_NAME", self.GKE_CLUSTER_NAME),
                ("GKE_ZONE", self.GKE_ZONE),
                ("GKE_PROJECT_ID", self.GKE_PROJECT_ID),
            ]
            if not val
        ]
        if missing:
            pytest.skip(f"GKE cluster upgrade test requires: {', '.join(missing)}")
        if not self.UPGRADE_K8S_VERSION:
            pytest.skip(
                "COMPACTION_UPGRADE_K8S_VERSION not set "
                "(e.g. export COMPACTION_UPGRADE_K8S_VERSION=1.35)"
            )

    def _gcloud(self, *args: str, timeout: int = 1800) -> str:
        cmd = [
            "gcloud",
            *args,
            "--project", self.GKE_PROJECT_ID,
        ]
        print(f"\n+ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.stdout:
            print(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(
                f"gcloud command failed (exit {result.returncode}):\n{result.stderr}"
            )
        return result.stdout.strip()

    def _upgrade_control_plane(self) -> None:
        self._gcloud(
            "container", "clusters", "upgrade", self.GKE_CLUSTER_NAME,
            "--master",
            f"--cluster-version={self.UPGRADE_K8S_VERSION}",
            "--zone", self.GKE_ZONE,
            "--quiet",
            timeout=1800,
        )

    def _upgrade_node_pool(self) -> None:
        self._gcloud(
            "container", "clusters", "upgrade", self.GKE_CLUSTER_NAME,
            f"--node-pool={self.GKE_NODE_POOL}",
            f"--cluster-version={self.UPGRADE_K8S_VERSION}",
            "--zone", self.GKE_ZONE,
            "--quiet",
            timeout=1800,
        )

    def _controller_ready(self, k8s_clients) -> bool:
        try:
            d = get_deployment(
                k8s_clients.apps, self.CONTROLLER_NAMESPACE, self.CONTROLLER_DEPLOYMENT
            )
        except Exception:
            return False
        s = d.status
        desired = d.spec.replicas or 1
        return (
            (s.observed_generation or 0) >= d.metadata.generation
            and (s.updated_replicas or 0) >= desired
            and (s.available_replicas or 0) >= desired
        )

    def _delete_policy(self, k8s_clients) -> None:
        try:
            k8s_clients.custom.delete_cluster_custom_object(
                GROUP, VERSION, "clustercompactionpolicies", self.POLICY_NAME
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    # ── test ──────────────────────────────────────────────────────────────────

    @pytest.mark.timeout(7200)
    def test_cluster_upgrade_preserves_compaction_state(self, k8s_clients, test_namespace):
        """Upgrade the GKE cluster k8s version and verify compaction state is preserved.

        Steps:
          1. Create WORKLOAD_COUNT Deployments + a ClusterCompactionPolicy.
          2. Verify all workload Pods were assigned schedulerName at admission pre-upgrade.
          3. Upgrade the control plane to COMPACTION_UPGRADE_K8S_VERSION (blocks ~15 min).
          4. Verify the policy CR still exists and workloads are still targeted.
          5. Upgrade the node pool (blocks ~15 min; controller pod restarts here).
          6. Wait for the controller to come back up.
          7. Verify workloads are still targeted and managedWorkloads count is correct.
        """
        self._require_gke()

        names = [f"{self.WORKLOAD_BASE}-{i}" for i in range(self.WORKLOAD_COUNT)]

        # ── 1. create workloads + policy ──────────────────────────────────────
        create_deployments_bulk(
            k8s_clients.apps,
            test_namespace,
            self.WORKLOAD_BASE,
            self.WORKLOAD_COUNT,
            cpu_request="10m",
            mem_request="32Mi",
            cpu_limit="50m",
            mem_limit="64Mi",
        )
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
                                    "values": names,
                                }
                            ]
                        },
                    },
                    "scheduler": {"useKubexScheduler": True},
                    "descheduler": {"enabled": False},
                },
            },
        )

        def all_have_intent() -> bool:
            return all(
                self.COMPACTION_INTENT_ANNOTATION
                in (get_deployment(k8s_clients.apps, test_namespace, name).metadata.annotations or {})
                for name in names
            )

        wait_for(all_have_intent, timeout=300, message="pre-upgrade: all workload intents reconciled")
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
            try:
                for name in names:
                    deployment = get_deployment(k8s_clients.apps, test_namespace, name)
                    if deployment.spec.template.spec.scheduler_name is not None:
                        return False
                    pods = k8s_clients.core.list_namespaced_pod(
                        test_namespace, label_selector=f"app={name}"
                    ).items
                    if not pods or not all(
                        pod.metadata.name not in old_pod_names and pod.spec.scheduler_name == self.SCHEDULER_NAME
                        for pod in pods
                    ):
                        return False
                return True
            except Exception:
                return False

        # ── 2. verify pre-upgrade targeting ───────────────────────────────────
        wait_for(all_targeted, timeout=300, message="pre-upgrade: all workloads targeted")
        pre_policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", self.POLICY_NAME)
        pre_managed = pre_policy["status"]["summary"]["managedWorkloads"]
        assert pre_managed == self.WORKLOAD_COUNT, (
            f"pre-upgrade: expected managedWorkloads={self.WORKLOAD_COUNT}, got {pre_managed}"
        )
        print(f"\nPre-upgrade state: {pre_managed}/{self.WORKLOAD_COUNT} workloads targeted")

        # ── 3. upgrade control plane ──────────────────────────────────────────
        print(f"\nUpgrading control plane → {self.UPGRADE_K8S_VERSION}")
        t_cp = time.time()
        self._upgrade_control_plane()
        cp_elapsed = time.time() - t_cp
        print(f"Control plane upgraded in {cp_elapsed:.0f}s")

        # ── 4. verify policy CR + targeting survived control plane upgrade ─────
        post_cp_policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", self.POLICY_NAME)
        assert post_cp_policy is not None, "Policy CR missing after control plane upgrade"
        wait_for(
            all_targeted,
            timeout=120,
            message="post-control-plane-upgrade: workloads still targeted",
        )
        print("Control plane upgrade: policy CR intact, workloads still targeted ✓")

        # ── 5. upgrade node pool ──────────────────────────────────────────────
        # The controller pod is evicted and rescheduled during this step.
        print(f"\nUpgrading node pool {self.GKE_NODE_POOL!r} → {self.UPGRADE_K8S_VERSION}")
        t_np = time.time()
        self._upgrade_node_pool()
        np_elapsed = time.time() - t_np
        print(f"Node pool upgraded in {np_elapsed:.0f}s")

        # ── 6. wait for controller to come back up ────────────────────────────
        wait_for(
            lambda: self._controller_ready(k8s_clients),
            timeout=300,
            message="controller ready after node pool upgrade",
        )
        print("Controller is Ready ✓")

        # ── 7. verify targeting preserved post-upgrade ────────────────────────
        wait_for(
            all_targeted,
            timeout=300,
            message="post-node-pool-upgrade: all workloads still targeted",
        )
        post_policy = get_crd(k8s_clients.custom, "clustercompactionpolicies", self.POLICY_NAME)
        post_managed = post_policy["status"]["summary"]["managedWorkloads"]

        print(f"\n=== Cluster Upgrade Test Results ===")
        print(f"Target k8s version:              {self.UPGRADE_K8S_VERSION}")
        print(f"Control plane upgrade time:      {cp_elapsed:.0f}s")
        print(f"Node pool upgrade time:          {np_elapsed:.0f}s")
        print(f"managedWorkloads pre-upgrade:    {pre_managed}/{self.WORKLOAD_COUNT}")
        print(f"managedWorkloads post-upgrade:   {post_managed}/{self.WORKLOAD_COUNT}")

        assert post_managed == self.WORKLOAD_COUNT, (
            f"post-upgrade: expected managedWorkloads={self.WORKLOAD_COUNT}, got {post_managed}"
        )
