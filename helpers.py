"""Shared utilities, manifest builders, and constants for the E2E test suite."""

import json
from copy import deepcopy
import subprocess
import socket
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
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

# Written by policy_reconciler to Deployments and propagated to pods by
# workloadrecommendation_controller.  Present on a running pod it means the
# StaticPolicy reconcile + annotation-sync cycle has completed, so the
# policyevaluation_controller can attempt an in-place resize.
STATIC_POLICY_ANNOTATION = "static.rightsizing.kubex.ai/desired-resource-requests"


# ---------------------------------------------------------------------------
# Generic Kubernetes / shell helpers
# ---------------------------------------------------------------------------


def kubectl(*args, context=None, check=True, input: str | None = None) -> str:
    """Run a kubectl command and return stdout.

    When `input` is provided, it is passed to stdin (useful with `-f -`).
    """
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def kubectl_diagnostics(*args, context=None) -> None:
    """Run a kubectl command and stream output without failing the caller."""
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += list(args)
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, capture_output=False, text=True, check=False)


def _get_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


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


def update_namespace_annotations(k8s_clients, namespace_name: str, mutate_annotations) -> None:
    """Retry namespace annotation updates through resource-version conflicts."""
    for attempt in range(5):
        namespace = k8s_clients.core.read_namespace(namespace_name)
        annotations = dict(namespace.metadata.annotations or {})
        mutate_annotations(annotations)
        namespace.metadata.annotations = annotations or None
        try:
            k8s_clients.core.replace_namespace(namespace_name, namespace)
            return
        except ApiException as exc:
            if exc.status != 409 or attempt == 4:
                raise
            time.sleep(1)
    raise RuntimeError(f"failed to update namespace annotations for {namespace_name}")


def clear_pause_annotations(annotations: dict[str, str]) -> None:
    """Remove namespace-level pause annotations used by the E2E tests."""
    annotations.pop("rightsizing.kubex.ai/pause-until", None)
    annotations.pop("rightsizing.kubex.ai/pause-reason", None)


def wait_for_vpa_recommendation(kube_context: str, namespace: str, vpa_name: str, timeout: int = 600) -> None:
    """Block until the VPA has generated its first recommendation (RecommendationProvided=True).

    The VPA recommender needs to observe at least one metrics sample before it
    can produce a recommendation.  On a warm cluster this typically happens
    within a minute; on a freshly provisioned kind node it can take several
    minutes.  The test uses this to ensure the VPA filter fires on the very
    first policyevaluation pass, making the "filter blocks resize" assertion
    deterministic.
    """

    cmd = [
        "kubectl",
        "--context",
        kube_context,
        "get",
        "vpa",
        vpa_name,
        "-n",
        namespace,
        "-o",
        "jsonpath={.status.conditions[?(@.type=='RecommendationProvided')].status}",
    ]
    deadline = time.time() + timeout
    last_stderr = ""
    while time.time() < deadline:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() == "True":
            return
        if result.returncode != 0:
            last_stderr = result.stderr.strip()
            lower_stderr = last_stderr.lower()
            if (
                "no kind" in lower_stderr
                or "server doesn't have a resource type" in lower_stderr
            ):
                raise RuntimeError(f"VPA CRD not available: {last_stderr}")
            if "notfound" not in lower_stderr and "not found" not in lower_stderr:
                raise RuntimeError(f"kubectl get VPA failed: {last_stderr}")
        time.sleep(10)
    raise TimeoutError(
        f"Timed out waiting for VPA recommendation for {namespace}/{vpa_name}. "
        f"Last kubectl error: {last_stderr}"
    )


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


@contextmanager
def port_forward_service(
    kube_context: str,
    namespace: str,
    service_name: str,
    local_port: int,
    remote_port: int,
):
    proc = subprocess.Popen(
        [
            "kubectl",
            "--context",
            kube_context,
            "port-forward",
            f"svc/{service_name}",
            f"{local_port}:{remote_port}",
            "-n",
            namespace,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.5):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"port-forward to 127.0.0.1:{local_port} did not become ready within 10s"
                    )
                time.sleep(0.2)
        yield
    finally:
        proc.terminate()


def mock_kubex_request(
    kube_context: str,
    namespace: str,
    method: str,
    path: str,
    payload: Any = None,
    local_port: int | None = None,
):
    data = None
    headers = {}
    port = local_port or _get_free_local_port()
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    with port_forward_service(
        kube_context,
        namespace,
        "kubex-stub",
        port,
        8080,
    ):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    body = response.read().decode("utf-8")
                    if not body:
                        return None
                    return json.loads(body)
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
            except urllib.error.URLError as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)


def reset_mock_kubex_state(kube_context: str, namespace: str) -> None:
    mock_kubex_request(kube_context, namespace, "POST", "/debug/reset", payload={})


def get_mock_kubex_state(kube_context: str, namespace: str) -> dict[str, Any]:
    state = mock_kubex_request(kube_context, namespace, "GET", "/debug/state")
    if not isinstance(state, dict):
        raise RuntimeError(f"unexpected mock state payload: {state!r}")
    return state


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
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels={"app": name}),
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
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels={"app": name}),
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


def create_stateful_set(
    apps: client.AppsV1Api,
    namespace: str,
    name: str,
    service_name: str,
    containers: list[dict[str, Any]],
    labels: dict[str, str] | None = None,
    replicas: int = 1,
) -> client.V1StatefulSet:
    """Create a minimal StatefulSet for affinity and replacement tests."""
    workload_labels = dict(labels or {"app": name})
    stateful_set = client.V1StatefulSet(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=workload_labels),
        spec=client.V1StatefulSetSpec(
            service_name=service_name,
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels=workload_labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=workload_labels),
                spec=client.V1PodSpec(
                    termination_grace_period_seconds=0,
                    containers=[
                        client.V1Container(
                            name=container["name"],
                            image=container.get("image", "registry.k8s.io/pause:3.10"),
                            command=container.get("command"),
                            args=container.get("args"),
                            resources=client.V1ResourceRequirements(
                                requests=container.get("requests"),
                                limits=container.get("limits"),
                            ),
                        )
                        for container in containers
                    ],
                ),
            ),
        ),
    )
    return apps.create_namespaced_stateful_set(namespace, stateful_set)


def delete_deployment(apps: client.AppsV1Api, namespace: str, name: str) -> None:
    """Delete a deployment and wait for full removal before returning."""
    try:
        apps.delete_namespaced_deployment(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return
        raise RuntimeError(f"failed to delete deployment {namespace}/{name}: {exc}") from exc
    deadline = time.time() + 30
    last_observed = None
    while time.time() < deadline:
        try:
            last_observed = apps.read_namespaced_deployment(name, namespace)
            time.sleep(1)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise RuntimeError(f"failed while waiting for deployment {namespace}/{name} removal: {exc}") from exc
    if last_observed is not None:
        status = last_observed.status
        raise RuntimeError(
            "timed out waiting for deployment "
            f"{namespace}/{name} to be removed; "
            f"observed phase={getattr(status, 'phase', None)!r}, "
            f"replicas={getattr(status, 'replicas', None)!r}, "
            f"available_replicas={getattr(status, 'available_replicas', None)!r}"
        )
    raise RuntimeError(f"timed out waiting for deployment {namespace}/{name} to be removed")


def delete_stateful_set(
    apps: client.AppsV1Api,
    core: client.CoreV1Api,
    namespace: str,
    name: str,
) -> None:
    """Delete a StatefulSet and wait for full removal before returning."""
    stateful_set = None
    try:
        stateful_set = apps.read_namespaced_stateful_set(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise RuntimeError(f"failed to read statefulset {namespace}/{name}: {exc}") from exc
    try:
        apps.delete_namespaced_stateful_set(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return
        raise RuntimeError(f"failed to delete statefulset {namespace}/{name}: {exc}") from exc

    deadline = time.time() + 30
    last_observed = None
    while time.time() < deadline:
        try:
            last_observed = apps.read_namespaced_stateful_set(name, namespace)
            time.sleep(1)
        except ApiException as exc:
            if exc.status == 404:
                break
            raise RuntimeError(
                f"failed while waiting for statefulset {namespace}/{name} removal: {exc}"
            ) from exc
    else:
        if last_observed is not None:
            status = last_observed.status
            raise RuntimeError(
                "timed out waiting for statefulset "
                f"{namespace}/{name} to be removed; "
                f"replicas={getattr(status, 'replicas', None)!r}, "
                f"ready_replicas={getattr(status, 'ready_replicas', None)!r}, "
                f"current_replicas={getattr(status, 'current_replicas', None)!r}"
            )
        raise RuntimeError(f"timed out waiting for statefulset {namespace}/{name} to be removed")

    if stateful_set and stateful_set.spec and stateful_set.spec.volume_claim_templates:
        replicas = (
            stateful_set.spec.replicas
            or getattr(stateful_set.status, "replicas", None)
            or 1
        )
        for template in stateful_set.spec.volume_claim_templates:
            claim_name = template.metadata.name
            if not claim_name:
                continue
            for ordinal in range(replicas):
                pvc_name = f"{claim_name}-{name}-{ordinal}"
                try:
                    core.delete_namespaced_persistent_volume_claim(pvc_name, namespace)
                except ApiException as exc:
                    if exc.status != 404:
                        raise RuntimeError(
                            f"failed to delete pvc {namespace}/{pvc_name}: {exc}"
                        ) from exc
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        core.read_namespaced_persistent_volume_claim(pvc_name, namespace)
                        time.sleep(1)
                    except ApiException as exc:
                        if exc.status == 404:
                            break
                        raise RuntimeError(
                            f"failed while waiting for pvc {namespace}/{pvc_name} removal: {exc}"
                        ) from exc
                else:
                    raise RuntimeError(
                        f"timed out waiting for pvc {namespace}/{pvc_name} to be removed"
                    )

    return


def delete_hpa(namespace: str, name: str) -> None:
    """Delete a HorizontalPodAutoscaler and block until it is fully gone (404).

    This mirrors ``delete_deployment`` so callers do not reimplement the
    async-deletion polling themselves, which would risk AlreadyExists races when
    immediately recreating an object with the same name.
    """
    hpa_api = client.AutoscalingV2Api()
    try:
        hpa_api.delete_namespaced_horizontal_pod_autoscaler(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return
        raise RuntimeError(f"failed to delete HPA {namespace}/{name}: {exc}") from exc
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            hpa_api.read_namespaced_horizontal_pod_autoscaler(name, namespace)
            time.sleep(1)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise RuntimeError(
                f"failed while waiting for HPA {namespace}/{name} removal: {exc}"
            ) from exc
    raise RuntimeError(f"timed out waiting for HPA {namespace}/{name} to be removed")


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


def get_stateful_set(apps: client.AppsV1Api, namespace: str, name: str) -> client.V1StatefulSet:
    """Fetch a StatefulSet."""
    return apps.read_namespaced_stateful_set(name, namespace)


def wait_for_stateful_set_ready(
    apps: client.AppsV1Api, namespace: str, name: str, min_replicas: int = 1
) -> None:
    """Wait until a StatefulSet reports the desired number of ready replicas."""
    wait_for(
        lambda: (
            (stateful_set := get_stateful_set(apps, namespace, name))
            and (stateful_set.status.ready_replicas or 0) >= min_replicas
        ),
        timeout=180,
        message=f"statefulset {namespace}/{name} readiness",
    )


def get_deployment_pod(core: client.CoreV1Api, namespace: str, deployment_name: str):
    """Return the single pod created for a deployment-style test workload."""
    pods = core.list_namespaced_pod(
        namespace,
        label_selector=f"app={deployment_name}",
    ).items
    if not pods:
        raise RuntimeError(f"no pod found for deployment {deployment_name}")
    active_pods = [pod for pod in pods if not pod.metadata.deletion_timestamp]
    candidates = active_pods or pods
    return max(
        candidates,
        key=lambda pod: (
            pod.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc),
            pod.metadata.name,
        ),
    )


def get_stateful_set_pod(core: client.CoreV1Api, namespace: str, stateful_set_name: str):
    """Return the current pod created for a single-replica StatefulSet test workload."""
    pods = core.list_namespaced_pod(
        namespace,
        label_selector=f"statefulset.kubernetes.io/pod-name={stateful_set_name}-0",
    ).items
    if not pods:
        raise RuntimeError(f"no pod found for statefulset {stateful_set_name}")
    active_pods = [pod for pod in pods if not pod.metadata.deletion_timestamp]
    candidates = active_pods or pods
    return max(
        candidates,
        key=lambda pod: (
            pod.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc),
            pod.metadata.name,
        ),
    )


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
    cpu_limit: str = None,
    mem_limit: str = None,
    weight: int = 0,
) -> dict:
    containers: dict[str, Any] = {}
    container_resources = _container_resources(cpu_request, mem_request, cpu_limit, mem_limit)
    if container_resources:
        containers["*"] = container_resources

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
    label_selector_app: str | None = None,
    cpu_request: str = None,
    mem_request: str = None,
    cpu_limit: str = None,
    mem_limit: str = None,
    namespace_operator: str = "In",
    namespace_values: list = None,
    weight: int = 0,
) -> dict:
    containers: dict[str, Any] = {}
    container_resources = _container_resources(cpu_request, mem_request, cpu_limit, mem_limit)
    if container_resources:
        containers["*"] = container_resources

    scope: dict[str, Any] = {
        "namespaceSelector": {
            "operator": namespace_operator,
            "values": namespace_values or [],
        }
    }
    if label_selector_app:
        scope["labelSelector"] = {"matchLabels": {"app": label_selector_app}}

    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "ClusterStaticPolicy",
        "metadata": {"name": name},
        "spec": {
            "automationStrategyRef": {"name": strategy_name},
            "weight": weight,
            "scope": scope,
            **({"resources": {"containers": containers}} if containers else {}),
        },
    }


def _container_resources(
    cpu_request: str = None,
    mem_request: str = None,
    cpu_limit: str = None,
    mem_limit: str = None,
) -> dict[str, Any] | None:
    if not (cpu_request or mem_request or cpu_limit or mem_limit):
        return None

    container_resources: dict[str, Any] = {}

    requests: dict[str, str] = {}
    if cpu_request:
        requests["cpu"] = cpu_request
    if mem_request:
        requests["memory"] = mem_request
    if requests:
        container_resources["requests"] = requests

    limits: dict[str, str] = {}
    if cpu_limit:
        limits["cpu"] = cpu_limit
    if mem_limit:
        limits["memory"] = mem_limit
    if limits:
        container_resources["limits"] = limits

    return container_resources


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

# ---------------------------------------------------------------------------
# StrimziPodSet helpers
# ---------------------------------------------------------------------------

STRIMZI_GROUP = "core.strimzi.io"
STRIMZI_VERSION = "v1beta2"
STRIMZI_VERSIONS = ("v1", "v1beta2")
STRIMZI_PLURAL = "strimzipodsets"
STRIMZI_KIND = "StrimziPodSet"


def _strimzipodset_labels(name: str) -> dict[str, str]:
    return {
        "app": name,
        "strimzi.io/cluster": name,
        "strimzi.io/kind": STRIMZI_KIND,
        "strimzi.io/name": name,
    }


def _strimzipodset_owner_reference(strimzipodset: dict) -> dict[str, Any]:
    metadata = strimzipodset.get("metadata", {})
    return {
        "apiVersion": strimzipodset.get("apiVersion", f"{STRIMZI_GROUP}/{STRIMZI_VERSION}"),
        "kind": strimzipodset.get("kind", STRIMZI_KIND),
        "name": metadata["name"],
        "uid": metadata["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def create_strimzipodset(
    custom_objects: client.CustomObjectsApi,
    namespace: str,
    name: str,
    replicas: int = 1,
    cpu_request: str = "100m",
    mem_request: str = "128Mi",
    cpu_limit: str = "200m",
    mem_limit: str = "256Mi",
    containers: list[dict] | None = None,
    version: str = STRIMZI_VERSION,
    core: client.CoreV1Api | None = None,
    create_owned_pods: bool = False,
) -> dict:
    """Create a StrimziPodSet resource for testing."""
    group = STRIMZI_GROUP
    plural = STRIMZI_PLURAL

    labels = _strimzipodset_labels(name)

    if containers is None:
        containers = [
            {
                "name": "app",
                "image": "busybox:latest",
                "command": ["sh", "-c", "sleep 3600"],
                "resources": {
                    "requests": {"cpu": cpu_request, "memory": mem_request},
                    "limits": {"cpu": cpu_limit, "memory": mem_limit},
                },
            }
        ]

    pods = []
    for i in range(replicas):
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{name}-{i}",
                "labels": labels,
            },
            "spec": {
                "containers": containers,
            },
        }
        pods.append(pod)

    strimzipodset = {
        "apiVersion": f"{group}/{version}",
        "kind": STRIMZI_KIND,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "selector": {"matchLabels": labels},
            "pods": pods,
        },
    }

    created = custom_objects.create_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
        body=strimzipodset,
    )

    if create_owned_pods:
        if core is None:
            raise ValueError("core client is required when create_owned_pods is True")

        owner_reference = _strimzipodset_owner_reference(created)
        for pod in created.get("spec", {}).get("pods", []):
            pod_body = deepcopy(pod)
            pod_name = pod_body.get("metadata", {}).get("name")
            if not pod_name:
                raise RuntimeError("StrimziPodSet pod is missing a name")

            pod_metadata = deepcopy(pod_body.get("metadata", {}))
            pod_metadata["name"] = pod_name
            pod_metadata["namespace"] = namespace
            pod_metadata["labels"] = labels
            pod_metadata["ownerReferences"] = [owner_reference]
            pod_body["metadata"] = pod_metadata

            pod_spec = deepcopy(pod_body.get("spec", {}))
            pod_body["spec"] = pod_spec
            pod_body["apiVersion"] = "v1"
            pod_body["kind"] = "Pod"

            core.create_namespaced_pod(namespace, pod_body)

            wait_for(
                lambda pod_name=pod_name: pod_is_ready(core.read_namespaced_pod(pod_name, namespace)),
                timeout=120,
                message=f"StrimziPodSet pod {namespace}/{pod_name} readiness",
            )

    return created


def get_strimzipodset(
    custom_objects: client.CustomObjectsApi,
    namespace: str,
    name: str,
    version: str = STRIMZI_VERSION,
) -> dict:
    """Get a StrimziPodSet resource."""
    group = STRIMZI_GROUP
    plural = STRIMZI_PLURAL

    return custom_objects.get_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
        name=name,
    )


def delete_strimzipodset(
    custom_objects: client.CustomObjectsApi,
    namespace: str,
    name: str,
    version: str = STRIMZI_VERSION,
) -> None:
    """Delete a StrimziPodSet and wait for it to be fully removed."""
    group = STRIMZI_GROUP
    plural = STRIMZI_PLURAL

    try:
        custom_objects.delete_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=plural,
            name=name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return
        raise RuntimeError(f"failed to delete StrimziPodSet {namespace}/{name}: {exc}") from exc
    
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            custom_objects.get_namespaced_custom_object(
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                name=name,
            )
            time.sleep(1)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise RuntimeError(
                f"failed while waiting for StrimziPodSet {namespace}/{name} removal: {exc}"
            ) from exc
    raise RuntimeError(f"timed out waiting for StrimziPodSet {namespace}/{name} to be removed")


def get_strimzipodset_resources(
    custom_objects: client.CustomObjectsApi,
    namespace: str,
    name: str,
    version: str = STRIMZI_VERSION,
) -> dict:
    """Return {container_name: {requests: {}, limits: {}}} for all containers in the first pod."""
    sps = get_strimzipodset(custom_objects, namespace, name, version=version)
    pods = sps.get("spec", {}).get("pods", [])
    if not pods:
        return {}
    
    first_pod = pods[0]
    containers = first_pod.get("spec", {}).get("containers", [])
    
    return {
        c["name"]: {
            "requests": dict(c.get("resources", {}).get("requests", {})),
            "limits": dict(c.get("resources", {}).get("limits", {})),
        }
        for c in containers
    }


def get_strimzipodset_pod(
    core: client.CoreV1Api,
    namespace: str,
    name: str,
) -> client.V1Pod:
    """Return the newest live pod belonging to a StrimziPodSet."""
    pods = core.list_namespaced_pod(namespace, label_selector=f"app={name}").items
    if not pods:
        raise RuntimeError(f"no pod found for StrimziPodSet {namespace}/{name}")

    live_pods = [pod for pod in pods if pod.metadata.deletion_timestamp is None]
    candidates = live_pods or pods
    return max(
        candidates,
        key=lambda pod: (
            pod.metadata.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc),
            pod.metadata.name,
        ),
    )
