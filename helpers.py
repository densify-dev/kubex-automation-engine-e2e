"""Shared utilities, manifest builders, and constants for the E2E test suite."""

import json
import subprocess
import time
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROUP = "rightsizing.kubex.ai"
VERSION = "v1alpha1"

POLL_INTERVAL = 2  # seconds
DEFAULT_TIMEOUT = 60  # seconds

RIGHTSIZING_ANNOTATION = "automation-webhook.kubex.ai/pod-rightsizing-info"


# ---------------------------------------------------------------------------
# Generic Kubernetes / shell helpers
# ---------------------------------------------------------------------------


def kubectl(*args, context=None, check=True) -> str:
    """Run a kubectl command and return stdout."""
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def wait_for(condition_fn, timeout=DEFAULT_TIMEOUT, interval=POLL_INTERVAL, message="condition"):
    """Poll condition_fn until it returns True or timeout expires."""
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            if condition_fn():
                return
        except Exception as exc:
            last_exc = exc
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {message}. Last exception: {last_exc}")


def get_crd(custom: client.CustomObjectsApi, plural: str, name: str, namespace: str = None) -> dict:
    """Fetch a CRD instance. Uses cluster scope when namespace is None."""
    if namespace:
        return custom.get_namespaced_custom_object(GROUP, VERSION, namespace, plural, name)
    return custom.get_cluster_custom_object(GROUP, VERSION, plural, name)


def apply_manifest(manifest: dict, context: str) -> str:
    """Apply a manifest dict via kubectl apply."""
    body = json.dumps(manifest) if isinstance(manifest, dict) else manifest
    cmd = ["kubectl", "--context", context, "apply", "-f", "-"]
    result = subprocess.run(cmd, input=body, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl apply failed:\n{result.stderr}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Deployment helpers
# ---------------------------------------------------------------------------


def create_deployment(
    apps: client.AppsV1Api,
    namespace: str,
    name: str,
    cpu_request: str = "100m",
    mem_request: str = "64Mi",
    cpu_limit: str = "200m",
    mem_limit: str = "128Mi",
) -> client.V1Deployment:
    """Create a minimal Deployment for testing resource mutation."""
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="app",
                            # Use a long-running image so the pod stays Running while
                            # the controller mutates resource requests.
                            image="registry.k8s.io/pause:3.10",
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": cpu_request, "memory": mem_request},
                                limits={"cpu": cpu_limit, "memory": mem_limit},
                            ),
                        )
                    ]
                ),
            ),
        ),
    )
    return apps.create_namespaced_deployment(namespace, deployment)


def create_multi_container_deployment(
    apps: client.AppsV1Api,
    namespace: str,
    name: str,
    containers: list[dict[str, Any]],
) -> client.V1Deployment:
    """Create a deployment with multiple named containers for recommendation tests."""
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name=container["name"],
                            image=container.get("image", "registry.k8s.io/pause:3.10"),
                            resources=client.V1ResourceRequirements(
                                requests=container["requests"],
                                limits=container["limits"],
                            ),
                        )
                        for container in containers
                    ]
                ),
            ),
        ),
    )
    return apps.create_namespaced_deployment(namespace, deployment)


def delete_deployment(apps: client.AppsV1Api, namespace: str, name: str) -> None:
    """Delete a deployment and wait for full removal before returning."""
    try:
        apps.delete_namespaced_deployment(name, namespace)
    except ApiException:
        return
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            apps.read_namespaced_deployment(name, namespace)
            time.sleep(1)
        except ApiException:
            return  # gone


def get_deployment_resources(apps: client.AppsV1Api, namespace: str, name: str) -> dict:
    """Return {container_name: {requests: {}, limits: {}}} for all containers."""
    d = apps.read_namespaced_deployment(name, namespace)
    return {
        c.name: {
            "requests": dict(c.resources.requests or {}),
            "limits": dict(c.resources.limits or {}),
        }
        for c in d.spec.template.spec.containers
    }


def get_deployment(apps: client.AppsV1Api, namespace: str, name: str) -> client.V1Deployment:
    """Fetch a deployment."""
    return apps.read_namespaced_deployment(name, namespace)


def get_deployment_pod(core: client.CoreV1Api, namespace: str, deployment_name: str):
    """Return the single pod created for a deployment-style test workload."""
    pods = core.list_namespaced_pod(
        namespace,
        label_selector=f"app={deployment_name}",
    ).items
    if not pods:
        raise RuntimeError(f"no pod found for deployment {deployment_name}")
    return sorted(pods, key=lambda pod: pod.metadata.name)[0]


def get_pod_resources(core: client.CoreV1Api, namespace: str, pod_name: str) -> dict:
    """Return {container_name: {requests: {}, limits: {}}} for a pod."""
    pod = core.read_namespaced_pod(pod_name, namespace)
    return {
        c.name: {
            "requests": dict((c.resources.requests or {})),
            "limits": dict((c.resources.limits or {})),
        }
        for c in pod.spec.containers
    }


def pod_is_ready(pod: client.V1Pod) -> bool:
    """Return True when the pod has a Ready condition."""
    return any(
        condition.type == "Ready" and condition.status == "True"
        for condition in (pod.status.conditions or [])
    )


# ---------------------------------------------------------------------------
# CRD manifest builders
# ---------------------------------------------------------------------------


def automation_strategy_manifest(
    name: str,
    namespace: str = None,
    cpu_downsize: bool = True,
    cpu_upsize: bool = True,
    mem_downsize: bool = True,
    mem_upsize: bool = True,
) -> dict:
    kind = "AutomationStrategy" if namespace else "ClusterAutomationStrategy"
    meta: dict = {"name": name}
    if namespace:
        meta["namespace"] = namespace
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": kind,
        "metadata": meta,
        "spec": {
            "enablement": {
                "cpu": {"requests": {"downsize": cpu_downsize, "upsize": cpu_upsize}},
                "memory": {"requests": {"downsize": mem_downsize, "upsize": mem_upsize}},
            }
        },
    }


def static_policy_manifest(
    name: str,
    namespace: str,
    strategy_name: str,
    strategy_namespace: str = None,
    label_selector_app: str | None = None,
    cpu_request: str = None,
    mem_request: str = None,
    weight: int = 0,
) -> dict:
    containers: dict[str, Any] = {}
    if cpu_request or mem_request:
        req: dict = {}
        if cpu_request:
            req["cpu"] = cpu_request
        if mem_request:
            req["memory"] = mem_request
        containers["*"] = {"requests": req}

    strategy_ref: dict = {"name": strategy_name}
    if strategy_namespace:
        strategy_ref["namespace"] = strategy_namespace

    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "StaticPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "automationStrategyRef": strategy_ref,
            "weight": weight,
            **(
                {"scope": {"labelSelector": {"matchLabels": {"app": label_selector_app}}}}
                if label_selector_app
                else {}
            ),
            **({"resources": {"containers": containers}} if containers else {}),
        },
    }


def cluster_static_policy_manifest(
    name: str,
    strategy_name: str,
    cpu_request: str = None,
    mem_request: str = None,
    namespace_operator: str = "In",
    namespace_values: list = None,
    weight: int = 0,
) -> dict:
    containers: dict[str, Any] = {}
    if cpu_request or mem_request:
        req: dict = {}
        if cpu_request:
            req["cpu"] = cpu_request
        if mem_request:
            req["memory"] = mem_request
        containers["*"] = {"requests": req}

    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "ClusterStaticPolicy",
        "metadata": {"name": name},
        "spec": {
            "automationStrategyRef": {"name": strategy_name},
            "weight": weight,
            "scope": {
                "namespaceSelector": {
                    "operator": namespace_operator,
                    "values": namespace_values or [],
                }
            },
            **({"resources": {"containers": containers}} if containers else {}),
        },
    }


def proactive_policy_manifest(
    name: str,
    namespace: str,
    strategy_name: str,
    max_analysis_age_days: int = 5,
) -> dict:
    # Note: ProactivePolicy.spec.automationStrategyRef only supports `name` (no namespace field)
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "ProactivePolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "automationStrategyRef": {"name": strategy_name},
            "safetyChecks": {"maxAnalysisAgeDays": max_analysis_age_days},
        },
    }
