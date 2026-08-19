"""Tests: ContainerArgsPolicy admission-time named argument mutation."""

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    create_stateful_set,
    delete_stateful_set,
    get_stateful_set,
    get_stateful_set_pod,
    pod_is_ready,
    wait_for,
    wait_for_stateful_set_ready,
)

CONTAINER_ARGS_ANNOTATION = "containerargspolicy.rightsizing.kubex.ai/pod-runtime-hook"


class TestContainerArgsPolicy:
    """Verify named argument operations apply only to matching workload containers."""

    POLICY_NAME = "e2e-container-args"
    MATCHING_SERVICE = "e2e-container-args-match"
    MATCHING_STATEFULSET = "e2e-container-args-match"
    NON_MATCHING_SERVICE = "e2e-container-args-miss"
    NON_MATCHING_STATEFULSET = "e2e-container-args-miss"

    def _delete_policy(self, k8s_clients) -> None:
        try:
            k8s_clients.custom.delete_cluster_custom_object(
                GROUP, VERSION, "containerargspolicies", self.POLICY_NAME
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            return
        wait_for(
            lambda: self._policy_missing(k8s_clients),
            timeout=30,
            message="ContainerArgsPolicy removal",
        )

    def _policy_missing(self, k8s_clients) -> bool:
        try:
            k8s_clients.custom.get_cluster_custom_object(
                GROUP, VERSION, "containerargspolicies", self.POLICY_NAME
            )
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise
        return False

    def _delete_service(self, k8s_clients, namespace: str, name: str) -> None:
        try:
            k8s_clients.core.delete_namespaced_service(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def _cleanup(self, k8s_clients, namespace: str) -> None:
        self._delete_policy(k8s_clients)
        for name in [self.MATCHING_STATEFULSET, self.NON_MATCHING_STATEFULSET]:
            delete_stateful_set(k8s_clients.apps, k8s_clients.core, namespace, name)
        for name in [self.MATCHING_SERVICE, self.NON_MATCHING_SERVICE]:
            self._delete_service(k8s_clients, namespace, name)

    def _create_service(self, k8s_clients, namespace: str, name: str, app: str) -> None:
        k8s_clients.core.create_namespaced_service(
            namespace,
            client.V1Service(
                metadata=client.V1ObjectMeta(name=name, namespace=namespace),
                spec=client.V1ServiceSpec(
                    cluster_ip="None",
                    selector={"app": app},
                    ports=[client.V1ServicePort(name="http", port=80, target_port=80)],
                ),
            ),
        )

    def _create_policy(self, k8s_clients, namespace: str, app: str) -> None:
        k8s_clients.custom.create_cluster_custom_object(
            GROUP,
            VERSION,
            "containerargspolicies",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "ContainerArgsPolicy",
                "metadata": {"name": self.POLICY_NAME},
                "spec": {
                    "scope": {
                        "workloadTypes": ["StatefulSet"],
                        "namespaceSelector": {"operator": "In", "values": [namespace]},
                        "labelSelector": {"matchLabels": {"app": app}},
                    },
                    "containers": {
                        "*": {
                            "args": [
                                {"name": "--flag", "value": "new"},
                                {"name": "--remove", "operation": "Remove"},
                                {"name": "--added"},
                            ]
                        }
                    },
                    "replaceExistingPods": False,
                    "weight": 100,
                },
            },
        )

    def _wait_for_policy_annotation(self, k8s_clients, namespace: str, name: str) -> None:
        def has_annotation():
            stateful_set = get_stateful_set(k8s_clients.apps, namespace, name)
            return CONTAINER_ARGS_ANNOTATION in (stateful_set.metadata.annotations or {})

        wait_for(
            has_annotation,
            timeout=180,
            message=f"ContainerArgsPolicy annotation on StatefulSet {namespace}/{name}",
        )

    def _replace_pod(self, k8s_clients, namespace: str, stateful_set_name: str):
        original = get_stateful_set_pod(k8s_clients.core, namespace, stateful_set_name)
        original_uid = original.metadata.uid
        k8s_clients.core.delete_namespaced_pod(original.metadata.name, namespace)
        replacement = None

        def replacement_ready():
            nonlocal replacement
            pod = get_stateful_set_pod(k8s_clients.core, namespace, stateful_set_name)
            if pod.metadata.uid == original_uid or not pod_is_ready(pod):
                return False
            replacement = pod
            return True

        wait_for(
            replacement_ready,
            timeout=180,
            message=f"replacement pod for statefulset {namespace}/{stateful_set_name}",
        )
        return replacement

    @pytest.fixture(autouse=True)
    def setup_teardown(self, k8s_clients, test_namespace):
        self._cleanup(k8s_clients, test_namespace)
        yield
        self._cleanup(k8s_clients, test_namespace)

    @pytest.mark.timeout(600)
    def test_matching_pod_gets_named_argument_operations(self, k8s_clients, test_namespace):
        self._create_policy(k8s_clients, test_namespace, self.MATCHING_STATEFULSET)
        self._create_service(k8s_clients, test_namespace, self.MATCHING_SERVICE, self.MATCHING_STATEFULSET)
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
                    "args": ["--before", "--flag=old", "--remove", "value", "--after", "keep"],
                }
            ],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, self.MATCHING_STATEFULSET)
        self._wait_for_policy_annotation(k8s_clients, test_namespace, self.MATCHING_STATEFULSET)

        pod = self._replace_pod(k8s_clients, test_namespace, self.MATCHING_STATEFULSET)
        assert pod.spec.containers[0].args == ["--before", "--flag=new", "--after", "keep", "--added"]

    @pytest.mark.timeout(600)
    def test_non_matching_pod_keeps_arguments_unchanged(self, k8s_clients, test_namespace):
        self._create_policy(k8s_clients, test_namespace, self.MATCHING_STATEFULSET)
        self._create_service(k8s_clients, test_namespace, self.MATCHING_SERVICE, self.MATCHING_STATEFULSET)
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            self.MATCHING_STATEFULSET,
            self.MATCHING_SERVICE,
            labels={"app": self.MATCHING_STATEFULSET},
            containers=[{"name": "app", "image": "registry.k8s.io/pause:3.10", "args": ["--flag=old"]}],
        )
        self._create_service(k8s_clients, test_namespace, self.NON_MATCHING_SERVICE, self.NON_MATCHING_STATEFULSET)
        create_stateful_set(
            k8s_clients.apps,
            test_namespace,
            self.NON_MATCHING_STATEFULSET,
            self.NON_MATCHING_SERVICE,
            labels={"app": self.NON_MATCHING_STATEFULSET},
            containers=[{"name": "app", "image": "registry.k8s.io/pause:3.10", "args": ["--flag=old"]}],
        )
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, self.MATCHING_STATEFULSET)
        wait_for_stateful_set_ready(k8s_clients.apps, test_namespace, self.NON_MATCHING_STATEFULSET)
        self._wait_for_policy_annotation(k8s_clients, test_namespace, self.MATCHING_STATEFULSET)

        assert CONTAINER_ARGS_ANNOTATION not in (
            get_stateful_set(k8s_clients.apps, test_namespace, self.NON_MATCHING_STATEFULSET).metadata.annotations
            or {}
        )
        pod = self._replace_pod(k8s_clients, test_namespace, self.NON_MATCHING_STATEFULSET)
        assert pod.spec.containers[0].args == ["--flag=old"]
