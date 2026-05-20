"""Tests: PodAffinityPolicy admission-time mutation for StatefulSet workloads."""

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_stateful_set,
    delete_stateful_set,
    get_crd,
    get_stateful_set,
    get_stateful_set_pod,
    pod_is_ready,
    wait_for_stateful_set_ready,
    wait_for,
)

POD_AFFINITY_ANNOTATION = "podaffinitypolicy.rightsizing.kubex.ai/pod-runtime-hook"
HOSTNAME_LABEL_KEY = "kubernetes.io/hostname"


class TestPodAffinityPolicy:
    """Verify PodAffinityPolicy only mutates matching StatefulSet pods."""

    POLICY_NAME = "e2e-pod-affinity"
    MATCHING_SERVICE = "e2e-pod-affinity-match"
    MATCHING_STATEFULSET = "e2e-pod-affinity-match"
    NON_MATCHING_SERVICE = "e2e-pod-affinity-miss"
    NON_MATCHING_STATEFULSET = "e2e-pod-affinity-miss"

    def _cleanup(self, k8s_clients, namespace: str) -> None:
        self._delete_policy(k8s_clients)
        for name in [self.MATCHING_STATEFULSET, self.NON_MATCHING_STATEFULSET]:
            delete_stateful_set(k8s_clients.apps, k8s_clients.core, namespace, name)
        for name in [self.MATCHING_SERVICE, self.NON_MATCHING_SERVICE]:
            self._delete_service(k8s_clients, namespace, name)

    def _delete_service(self, k8s_clients, namespace: str, name: str) -> None:
        try:
            k8s_clients.core.delete_namespaced_service(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            return

        wait_for(
            lambda: self._service_missing(k8s_clients, namespace, name),
            timeout=30,
            message=f"service {namespace}/{name} removal",
        )

    def _service_missing(self, k8s_clients, namespace: str, name: str) -> bool:
        try:
            k8s_clients.core.read_namespaced_service(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise
        return False

    def _delete_policy(self, k8s_clients) -> None:
        try:
            k8s_clients.custom.delete_cluster_custom_object(
                GROUP, VERSION, "podaffinitypolicies", self.POLICY_NAME
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            return

        wait_for(
            lambda: self._policy_missing(k8s_clients),
            timeout=30,
            message="PodAffinityPolicy removal",
        )

    def _policy_missing(self, k8s_clients) -> bool:
        try:
            k8s_clients.custom.get_cluster_custom_object(
                GROUP, VERSION, "podaffinitypolicies", self.POLICY_NAME
            )
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise
        return False

    def _create_headless_service(self, k8s_clients, namespace: str, name: str, app: str) -> None:
        service = client.V1Service(
            metadata=client.V1ObjectMeta(name=name, namespace=namespace),
            spec=client.V1ServiceSpec(
                cluster_ip="None",
                selector={"app": app},
                ports=[client.V1ServicePort(name="http", port=80, target_port=80)],
            ),
        )
        k8s_clients.core.create_namespaced_service(namespace, service)

    def _create_policy(self, k8s_clients, namespace: str, app: str, nodes: list[str]) -> None:
        k8s_clients.custom.create_cluster_custom_object(
            GROUP,
            VERSION,
            "podaffinitypolicies",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "PodAffinityPolicy",
                "metadata": {"name": self.POLICY_NAME},
                "spec": {
                    "scope": {
                        "workloadTypes": ["StatefulSet"],
                        "namespaceSelector": {"operator": "In", "values": [namespace]},
                        "labelSelector": {"matchLabels": {"app": app}},
                    },
                    "affinity": {"nodes": nodes},
                    "weight": 10,
                },
            },
        )

    def _wait_for_webhook_health(self, k8s_clients) -> None:
        def webhook_healthy():
            gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
            conditions = gc.get("status", {}).get("conditions", [])
            return any(
                condition["type"] == "PodAdmissionWebhookHealthy"
                and condition["status"] == "True"
                for condition in conditions
            )

        wait_for(webhook_healthy, timeout=120, message="PodAdmissionWebhookHealthy condition")

    def _live_node_hostnames(self, k8s_clients) -> list[str]:
        hostnames = sorted(
            {
                node.metadata.labels.get(HOSTNAME_LABEL_KEY)
                for node in k8s_clients.core.list_node().items
                if node.metadata.labels and node.metadata.labels.get(HOSTNAME_LABEL_KEY)
            }
        )
        if not hostnames:
            raise RuntimeError("cluster nodes are missing kubernetes.io/hostname labels")
        return hostnames[: min(2, len(hostnames))]

    def _wait_for_stateful_set_policy_annotation(self, k8s_clients, namespace: str, name: str) -> None:
        def stateful_set_has_policy_annotation():
            stateful_set = get_stateful_set(k8s_clients.apps, namespace, name)
            annotations = stateful_set.metadata.annotations or {}
            return POD_AFFINITY_ANNOTATION in annotations

        wait_for(
            stateful_set_has_policy_annotation,
            timeout=180,
            message=f"PodAffinityPolicy annotation on StatefulSet {namespace}/{name}",
        )

    def _replace_stateful_set_pod(self, k8s_clients, namespace: str, name: str):
        original = get_stateful_set_pod(k8s_clients.core, namespace, name)
        original_uid = original.metadata.uid
        k8s_clients.core.delete_namespaced_pod(original.metadata.name, namespace)

        replacement = None

        def replacement_ready():
            nonlocal replacement
            pod = get_stateful_set_pod(k8s_clients.core, namespace, name)
            if pod.metadata.uid == original_uid:
                return False
            if not pod_is_ready(pod):
                return False
            replacement = pod
            return True

        wait_for(
            replacement_ready,
            timeout=180,
            message=f"replacement pod for statefulset {namespace}/{name}",
        )
        return replacement

    def _matching_preferred_hostname_terms(self, pod) -> list[client.V1PreferredSchedulingTerm]:
        affinity = pod.spec.affinity
        if affinity is None or affinity.node_affinity is None:
            return []
        preferred = affinity.node_affinity.preferred_during_scheduling_ignored_during_execution
        if not preferred:
            return []
        matches = []
        for term in preferred:
            expressions = term.preference.match_expressions or []
            if len(expressions) != 1:
                continue
            expression = expressions[0]
            if expression.key != HOSTNAME_LABEL_KEY:
                continue
            if expression.operator != "In":
                continue
            matches.append(term)
        return matches

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        self._cleanup(k8s_clients, test_namespace)

        yield

        self._cleanup(k8s_clients, test_namespace)

    @pytest.mark.timeout(600)
    def test_matching_statefulset_pod_gets_preferred_hostname_affinity(
        self, k8s_clients, test_namespace
    ):
        self._wait_for_webhook_health(k8s_clients)
        nodes = self._live_node_hostnames(k8s_clients)
        self._create_policy(k8s_clients, test_namespace, self.MATCHING_STATEFULSET, nodes)
        self._create_headless_service(
            k8s_clients, test_namespace, self.MATCHING_SERVICE, self.MATCHING_STATEFULSET
        )
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            self.MATCHING_STATEFULSET,
            self.MATCHING_SERVICE,
            labels={"app": self.MATCHING_STATEFULSET},
            containers=[
                {
                    "name": "app",
                    "image": "registry.k8s.io/pause:3.10",
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, self.MATCHING_STATEFULSET)
        self._wait_for_stateful_set_policy_annotation(
            k8s_clients, test_namespace, self.MATCHING_STATEFULSET
        )

        pod = self._replace_stateful_set_pod(
            k8s_clients, test_namespace, self.MATCHING_STATEFULSET
        )
        matching_terms = self._matching_preferred_hostname_terms(pod)
        assert len(matching_terms) == 1
        assert matching_terms[0].weight == 100
        expression = matching_terms[0].preference.match_expressions[0]
        assert expression.key == HOSTNAME_LABEL_KEY
        assert expression.operator == "In"
        assert set(expression.values or []) == set(nodes)

    @pytest.mark.timeout(600)
    def test_non_matching_statefulset_pod_keeps_affinity_unchanged(
        self, k8s_clients, test_namespace
    ):
        self._wait_for_webhook_health(k8s_clients)
        nodes = self._live_node_hostnames(k8s_clients)
        self._create_policy(k8s_clients, test_namespace, self.MATCHING_STATEFULSET, nodes)
        self._create_headless_service(
            k8s_clients, test_namespace, self.MATCHING_SERVICE, self.MATCHING_STATEFULSET
        )
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            self.MATCHING_STATEFULSET,
            self.MATCHING_SERVICE,
            labels={"app": self.MATCHING_STATEFULSET},
            containers=[
                {
                    "name": "app",
                    "image": "registry.k8s.io/pause:3.10",
                }
            ],
        )
        self._create_headless_service(
            k8s_clients, test_namespace, self.NON_MATCHING_SERVICE, self.NON_MATCHING_STATEFULSET
        )
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            self.NON_MATCHING_STATEFULSET,
            self.NON_MATCHING_SERVICE,
            labels={"app": self.NON_MATCHING_STATEFULSET},
            containers=[
                {
                    "name": "app",
                    "image": "registry.k8s.io/pause:3.10",
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, self.MATCHING_STATEFULSET)
        wait_for_stateful_set_ready(
            k8s_clients.apps, test_namespace, self.NON_MATCHING_STATEFULSET
        )
        self._wait_for_stateful_set_policy_annotation(
            k8s_clients, test_namespace, self.MATCHING_STATEFULSET
        )

        matching_pod = self._replace_stateful_set_pod(
            k8s_clients, test_namespace, self.MATCHING_STATEFULSET
        )
        matching_terms = self._matching_preferred_hostname_terms(matching_pod)
        assert len(matching_terms) == 1
        assert matching_terms[0].weight == 100
        stateful_set = get_stateful_set(
            k8s_clients.apps, test_namespace, self.NON_MATCHING_STATEFULSET
        )
        assert POD_AFFINITY_ANNOTATION not in (stateful_set.metadata.annotations or {})

        pod = self._replace_stateful_set_pod(
            k8s_clients, test_namespace, self.NON_MATCHING_STATEFULSET
        )
        assert self._matching_preferred_hostname_terms(pod) == []
