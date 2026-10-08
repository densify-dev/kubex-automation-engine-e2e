"""E2E tests for StrimziPodSet workload support."""

from __future__ import annotations

import json
import time
import uuid

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    STATIC_POLICY_ANNOTATION,
    STRIMZI_VERSIONS,
    VERSION,
    automation_strategy_manifest,
    create_strimzipodset,
    delete_strimzipodset,
    get_strimzipodset,
    get_strimzipodset_pod,
    static_policy_manifest,
    wait_for,
)

STATIC_POLICY_LIMITS_ANNOTATION = "static.rightsizing.kubex.ai/desired-resource-limits"


def _unique_name(prefix: str, version: str) -> str:
    return f"{prefix}-{version}-{uuid.uuid4().hex[:8]}"


def _delete_custom_object(k8s_clients, namespace: str, plural: str, name: str) -> None:
    try:
        k8s_clients.custom.delete_namespaced_custom_object(GROUP, VERSION, namespace, plural, name)
    except ApiException as exc:
        if exc.status != 404:
            raise


@pytest.fixture(scope="module", autouse=True)
def _ensure_strimzipodset_crd(ensure_strimzipodset_crd):
    return ensure_strimzipodset_crd


@pytest.fixture(params=STRIMZI_VERSIONS)
def strimzi_version(request):
    return request.param


@pytest.fixture
def strimzipodset_name(strimzi_version):
    return _unique_name("e2e-strimzi", strimzi_version)


@pytest.fixture
def strategy_name(k8s_clients, test_namespace, strimzi_version):
    name = _unique_name("e2e-strimzi-strategy", strimzi_version)
    k8s_clients.custom.create_namespaced_custom_object(
        GROUP,
        VERSION,
        test_namespace,
        "automationstrategies",
        automation_strategy_manifest(name, test_namespace),
    )
    yield name
    _delete_custom_object(k8s_clients, test_namespace, "automationstrategies", name)


@pytest.fixture
def strimzipodset(k8s_clients, test_namespace, strimzipodset_name, strimzi_version):
    sps = create_strimzipodset(
        k8s_clients.custom,
        namespace=test_namespace,
        name=strimzipodset_name,
        replicas=1,
        cpu_request="100m",
        mem_request="128Mi",
        cpu_limit="200m",
        mem_limit="256Mi",
        version=strimzi_version,
        core=k8s_clients.core,
        create_owned_pods=True,
    )
    yield sps
    delete_strimzipodset(
        k8s_clients.custom,
        test_namespace,
        strimzipodset_name,
        version=strimzi_version,
    )


def _wait_for_static_policy_annotation(k8s_clients, test_namespace: str, name: str, version: str) -> None:
    def workload_has_annotation():
        sps = get_strimzipodset(k8s_clients.custom, test_namespace, name, version=version)
        annotations = sps.get("metadata", {}).get("annotations", {})
        return STATIC_POLICY_ANNOTATION in annotations

    wait_for(
        workload_has_annotation,
        timeout=120,
        message=f"StaticPolicy annotation on StrimziPodSet {test_namespace}/{name}",
    )


def _wait_for_owned_pod_annotation(k8s_clients, test_namespace: str, name: str) -> None:
    def pod_has_annotation():
        pod = get_strimzipodset_pod(k8s_clients.core, test_namespace, name)
        annotations = pod.metadata.annotations or {}
        return STATIC_POLICY_ANNOTATION in annotations

    wait_for(
        pod_has_annotation,
        timeout=120,
        message=f"StaticPolicy annotation on owned Pod for {test_namespace}/{name}",
    )


def _annotation_payload(annotations: dict[str, str] | None, annotation_key: str = STATIC_POLICY_ANNOTATION) -> dict:
    raw = (annotations or {}).get(annotation_key)
    assert raw is not None
    return json.loads(raw)


def _create_static_policy(
    k8s_clients,
    test_namespace: str,
    policy_name: str,
    strategy_name: str,
    workload_name: str,
    workload_types: list[str] | None = None,
    cpu_request: str = "200m",
    mem_request: str = "256Mi",
    cpu_limit: str = "400m",
    mem_limit: str = "512Mi",
):
    policy_manifest = static_policy_manifest(
        name=policy_name,
        namespace=test_namespace,
        strategy_name=strategy_name,
        label_selector_app=workload_name,
        cpu_request=cpu_request,
        mem_request=mem_request,
        cpu_limit=cpu_limit,
        mem_limit=mem_limit,
    )
    if workload_types is not None:
        policy_manifest.setdefault("spec", {}).setdefault("scope", {})["workloadTypes"] = workload_types

    k8s_clients.custom.create_namespaced_custom_object(
        GROUP,
        VERSION,
        test_namespace,
        "staticpolicies",
        policy_manifest,
    )


def _delete_static_policy(k8s_clients, test_namespace: str, policy_name: str) -> None:
    _delete_custom_object(k8s_clients, test_namespace, "staticpolicies", policy_name)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            k8s_clients.custom.get_namespaced_custom_object(
                GROUP,
                VERSION,
                test_namespace,
                "staticpolicies",
                policy_name,
            )
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for StaticPolicy {test_namespace}/{policy_name} deletion")


@pytest.mark.timeout(600)
def test_static_policy_applies_to_strimzipodset(
    k8s_clients,
    test_namespace,
    strimzi_version,
    strimzipodset_name,
    strimzipodset,
    strategy_name,
):
    """StaticPolicy should apply to a StrimziPodSet when explicitly opted in."""
    policy_name = _unique_name("e2e-strimzi-policy", strimzi_version)
    _create_static_policy(
        k8s_clients,
        test_namespace,
        policy_name,
        strategy_name,
        strimzipodset_name,
        workload_types=["StrimziPodSet"],
    )

    try:
        _wait_for_static_policy_annotation(k8s_clients, test_namespace, strimzipodset_name, strimzi_version)
        _wait_for_owned_pod_annotation(k8s_clients, test_namespace, strimzipodset_name)

        sps = get_strimzipodset(k8s_clients.custom, test_namespace, strimzipodset_name, version=strimzi_version)
        workload_annotations = sps.get("metadata", {}).get("annotations", {})
        workload_payload = _annotation_payload(workload_annotations)
        assert workload_payload["containers"]["*"]["cpu"] == "200m"
        assert workload_payload["containers"]["*"]["memory"] == "256Mi"
        limits_payload = _annotation_payload(workload_annotations, STATIC_POLICY_LIMITS_ANNOTATION)
        assert limits_payload["containers"]["*"]["cpu"] == "400m"
        assert limits_payload["containers"]["*"]["memory"] == "512Mi"

        pod = get_strimzipodset_pod(k8s_clients.core, test_namespace, strimzipodset_name)
        pod_annotations = pod.metadata.annotations or {}
        pod_payload = _annotation_payload(pod_annotations)
        assert pod_payload["containers"]["*"]["cpu"] == "200m"
        assert pod_payload["containers"]["*"]["memory"] == "256Mi"
        pod_limits_payload = _annotation_payload(pod_annotations, STATIC_POLICY_LIMITS_ANNOTATION)
        assert pod_limits_payload["containers"]["*"]["cpu"] == "400m"
        assert pod_limits_payload["containers"]["*"]["memory"] == "512Mi"
    finally:
        _delete_static_policy(k8s_clients, test_namespace, policy_name)


@pytest.mark.timeout(300)
def test_strimzipodset_selector_persisted(
    k8s_clients,
    test_namespace,
    strimzi_version,
    strimzipodset_name,
    strimzipodset,
):
    """The StrimziPodSet selector should remain stable."""
    sps = get_strimzipodset(k8s_clients.custom, test_namespace, strimzipodset_name, version=strimzi_version)
    selector = sps.get("spec", {}).get("selector", {}).get("matchLabels", {})

    assert selector["app"] == strimzipodset_name
    assert selector["strimzi.io/cluster"] == strimzipodset_name
    assert selector["strimzi.io/kind"] == "StrimziPodSet"


@pytest.mark.timeout(600)
def test_static_policy_requires_strimzipodset_opt_in(
    k8s_clients,
    test_namespace,
    strimzi_version,
    strimzipodset_name,
    strimzipodset,
    strategy_name,
):
    """StaticPolicy should ignore StrimziPodSet when it is not explicitly opted in."""
    policy_name = _unique_name("e2e-strimzi-unscoped-policy", strimzi_version)
    _create_static_policy(
        k8s_clients,
        test_namespace,
        policy_name,
        strategy_name,
        strimzipodset_name,
        workload_types=None,
    )

    try:
        time.sleep(10)

        sps = get_strimzipodset(k8s_clients.custom, test_namespace, strimzipodset_name, version=strimzi_version)
        pod = get_strimzipodset_pod(k8s_clients.core, test_namespace, strimzipodset_name)

        assert STATIC_POLICY_ANNOTATION not in (sps.get("metadata", {}).get("annotations", {}))
        assert STATIC_POLICY_ANNOTATION not in (pod.metadata.annotations or {})
    finally:
        _delete_static_policy(k8s_clients, test_namespace, policy_name)


@pytest.mark.timeout(600)
def test_strimzipodset_multi_container(
    k8s_clients,
    test_namespace,
    strimzi_version,
    strategy_name,
):
    """StaticPolicy should handle StrimziPodSet pods with multiple containers."""
    sps_name = _unique_name("e2e-strimzi-multi", strimzi_version)
    policy_name = _unique_name("e2e-strimzi-multi-policy", strimzi_version)
    container_resources = [
        {
            "name": "kafka",
            "image": "busybox:latest",
            "command": ["sh", "-c", "sleep 3600"],
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "200m", "memory": "256Mi"},
            },
        },
        {
            "name": "sidecar",
            "image": "busybox:latest",
            "command": ["sh", "-c", "sleep 3600"],
            "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"cpu": "100m", "memory": "128Mi"},
            },
        },
    ]

    create_strimzipodset(
        k8s_clients.custom,
        namespace=test_namespace,
        name=sps_name,
        replicas=1,
        containers=container_resources,
        version=strimzi_version,
        core=k8s_clients.core,
        create_owned_pods=True,
    )

    _create_static_policy(
        k8s_clients,
        test_namespace,
        policy_name,
        strategy_name,
        sps_name,
        workload_types=["StrimziPodSet"],
        cpu_request="250m",
        mem_request="256Mi",
        cpu_limit="450m",
        mem_limit="512Mi",
    )

    try:
        _wait_for_static_policy_annotation(k8s_clients, test_namespace, sps_name, strimzi_version)
        _wait_for_owned_pod_annotation(k8s_clients, test_namespace, sps_name)

        sps = get_strimzipodset(k8s_clients.custom, test_namespace, sps_name, version=strimzi_version)
        workload_annotations = sps.get("metadata", {}).get("annotations", {})
        workload_payload = _annotation_payload(workload_annotations)
        assert workload_payload["containers"]["*"]["cpu"] == "250m"
        assert workload_payload["containers"]["*"]["memory"] == "256Mi"
        limits_payload = _annotation_payload(workload_annotations, STATIC_POLICY_LIMITS_ANNOTATION)
        assert limits_payload["containers"]["*"]["cpu"] == "450m"
        assert limits_payload["containers"]["*"]["memory"] == "512Mi"

        pod = get_strimzipodset_pod(k8s_clients.core, test_namespace, sps_name)
        pod_annotations = pod.metadata.annotations or {}
        pod_payload = _annotation_payload(pod_annotations)
        assert pod_payload["containers"]["*"]["cpu"] == "250m"
        assert pod_payload["containers"]["*"]["memory"] == "256Mi"
        pod_limits_payload = _annotation_payload(pod_annotations, STATIC_POLICY_LIMITS_ANNOTATION)
        assert pod_limits_payload["containers"]["*"]["cpu"] == "450m"
        assert pod_limits_payload["containers"]["*"]["memory"] == "512Mi"
    finally:
        _delete_static_policy(k8s_clients, test_namespace, policy_name)
        delete_strimzipodset(
            k8s_clients.custom,
            test_namespace,
            sps_name,
            version=strimzi_version,
        )
