"""Experimental vLLM adaptation checks on a real single-GPU cluster."""

import json
import socket
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest
from kubernetes.client.rest import ApiException

from helpers import (
    GROUP,
    VERSION,
    apply_manifest,
    get_crd,
    namespace_gone,
    port_forward_service,
    prometheus_query,
    wait_for,
    wait_for_pod_ready,
)

pytestmark = pytest.mark.gpu_suite

IMAGE = "vllm/vllm-openai@sha256:014a95f21c9edf6abe0aea6b07353f96baa4ec291c427bb1176dc7c93a85845c"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
MODEL_NAME = "qwen2-5-0-5b-e2e"
PROMETHEUS_URL = "http://prometheus.monitoring.svc:9090"
BATCH_ARG = "--max-num-batched-tokens"

COUNTER_FAMILIES = (
    "vllm:generation_tokens_total",
    "vllm:request_success_total",
)
HISTOGRAM_COUNT_FAMILIES = (
    "vllm:time_to_first_token_seconds_count",
    "vllm:inter_token_latency_seconds_count",
    "vllm:request_prompt_tokens_count",
    "vllm:request_generation_tokens_count",
)
GAUGE_FAMILIES = (
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _RequestLoad:
    def __init__(self, url: str, workers: int = 32):
        self.url = url
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.repetitions = 2
        self.request_errors = 0
        self.last_error = ""
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.futures = [self.executor.submit(self._run) for _ in range(workers)]

    def set_prompt_mix(self, repetitions: int) -> None:
        with self.lock:
            self.repetitions = repetitions

    def _run(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                repetitions = self.repetitions
            payload = json.dumps(
                {
                    "model": MODEL_NAME,
                    "prompt": "The controlled request says hello. " * repetitions,
                    "max_tokens": 8,
                    "temperature": 0,
                }
            ).encode()
            request = urllib.request.Request(
                f"{self.url}/v1/completions",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    response.read()
            except Exception as exc:  # Requests overlap pod replacement during trials.
                with self.lock:
                    self.request_errors += 1
                    self.last_error = str(exc)
                self.stop.wait(0.2)

    def close(self) -> None:
        self.stop.set()
        self.executor.shutdown(wait=True, cancel_futures=True)


def _keep_port_forward(
    kube_context: str, namespace: str, service: str, port: int, stop: threading.Event
) -> None:
    command = [
        "kubectl",
        "--context",
        kube_context,
        "port-forward",
        f"svc/{service}",
        f"{port}:8000",
        "-n",
        namespace,
    ]
    while not stop.is_set():
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while process.poll() is None and not stop.wait(0.2):
            pass
        if process.poll() is None:
            process.terminate()
        process.wait()
        stop.wait(1)


@contextmanager
def _load_driver(kube_context: str, namespace: str, service: str):
    port = _free_port()
    stop_forward = threading.Event()
    forwarder = threading.Thread(
        target=_keep_port_forward,
        args=(kube_context, namespace, service, port, stop_forward),
    )
    forwarder.start()
    driver = _RequestLoad(f"http://127.0.0.1:{port}")
    try:
        yield driver
    except Exception as exc:
        if driver.request_errors:
            raise AssertionError(
                f"{exc}; inference requests failed {driver.request_errors} times, "
                f"latest error: {driver.last_error}"
            ) from exc
        raise
    finally:
        driver.close()
        stop_forward.set()
        forwarder.join()


def _reload_prometheus(kube_context: str) -> None:
    port = _free_port()
    with port_forward_service(kube_context, "monitoring", "prometheus", port, 9090):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/-/reload", data=b"", method="POST"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()


def _metric_values(kube_context: str, namespace: str, deployment: str) -> dict[str, float]:
    selector = f'namespace="{namespace}",pod=~"{deployment}-.*"'
    values = {}
    for family in (*COUNTER_FAMILIES, *HISTOGRAM_COUNT_FAMILIES):
        query = f"sum(rate({family}{{{selector}}}[1m]))"
        rows = prometheus_query(kube_context, "monitoring", query)["data"]["result"]
        if rows:
            values[family] = float(rows[0]["value"][1])
    for family in GAUGE_FAMILIES:
        query = f"max({family}{{{selector}}})"
        rows = prometheus_query(kube_context, "monitoring", query)["data"]["result"]
        if rows:
            values[family] = float(rows[0]["value"][1])
    return values


def test_metric_values_maps_each_query_to_its_family(monkeypatch):
    families = COUNTER_FAMILIES + HISTOGRAM_COUNT_FAMILIES + GAUGE_FAMILIES
    queries = []

    def query(_context, _namespace, expression):
        queries.append(expression)
        # Prometheus rate() results need not include __name__.
        return {"data": {"result": [{"metric": {}, "value": [0, str(len(queries))]}]}}

    monkeypatch.setitem(globals(), "prometheus_query", query)
    values = _metric_values("context", "namespace", "server")

    assert values == {family: float(index) for index, family in enumerate(families, 1)}
    assert len(queries) == len(families)
    assert all(" or " not in expression for expression in queries)


def _prompt_mean(kube_context: str, namespace: str, deployment: str) -> float | None:
    selector = f'namespace="{namespace}",pod=~"{deployment}-.*"'
    query = (
        f"sum(rate(vllm:request_prompt_tokens_sum{{{selector}}}[60s])) / "
        f"sum(rate(vllm:request_prompt_tokens_count{{{selector}}}[60s]))"
    )
    rows = prometheus_query(kube_context, "monitoring", query)["data"]["result"]
    return float(rows[0]["value"][1]) if rows else None


def _wait_for_policy(custom, name: str, predicate, timeout: int, message: str) -> dict:
    last_policy = None

    def matches() -> bool:
        nonlocal last_policy
        last_policy = get_crd(custom, "vllmoptimizationpolicies", name)
        return predicate(last_policy)

    wait_for(matches, timeout=timeout, interval=5, message=message)
    return last_policy


def _argument_value(arguments: list[dict], name: str = BATCH_ARG) -> str | None:
    for argument in arguments:
        if argument.get("name") == name:
            return argument.get("value")
    return None


def _configuration_value(configuration: dict | None) -> str | None:
    return _argument_value((configuration or {}).get("arguments", []))


def _ready_pod_with_value(core, namespace: str, deployment: str, expected: str) -> bool:
    pods = core.list_namespaced_pod(namespace, label_selector=f"app={deployment}").items
    for pod in pods:
        ready = any(
            status.name == "vllm" and status.ready
            for status in (pod.status.container_statuses or [])
        )
        if not ready:
            continue
        container = next(
            (container for container in pod.spec.containers if container.name == "vllm"), None
        )
        if container is None:
            continue
        args = container.args or []
        for index, arg in enumerate(args):
            if arg.startswith(f"{BATCH_ARG}=") and arg.split("=", 1)[1] == expected:
                return True
            if arg == BATCH_ARG and index + 1 < len(args) and args[index + 1] == expected:
                return True
    return False


def _gpu_node_available(core) -> bool:
    return any(
        int(node.status.allocatable.get("nvidia.com/gpu", "0")) > 0
        for node in core.list_node().items
    )


def _deployment_manifest(namespace: str, name: str) -> str:
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/path: /metrics
        prometheus.io/port: "8000"
    spec:
      terminationGracePeriodSeconds: 30
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      containers:
        - name: vllm
          image: {IMAGE}
          imagePullPolicy: IfNotPresent
          args:
            - --model
            - {MODEL}
            - --revision
            - {MODEL_REVISION}
            - --served-model-name
            - {MODEL_NAME}
            - --dtype
            - half
            - --max-model-len
            - "1024"
            - {BATCH_ARG}=4096
            - --max-num-seqs=8
            - --gpu-memory-utilization=0.9
            - --enable-chunked-prefill
          ports:
            - name: http
              containerPort: 8000
          readinessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 10
            failureThreshold: 90
          resources:
            requests:
              cpu: "2"
              memory: 8Gi
              nvidia.com/gpu: "1"
            limits:
              cpu: "4"
              memory: 12Gi
              nvidia.com/gpu: "1"
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {namespace}
spec:
  selector:
    app: {name}
  ports:
    - name: http
      port: 8000
      targetPort: http
"""


def _policy_manifest(name: str, namespace: str, workload: str) -> str:
    return f"""\
apiVersion: {GROUP}/{VERSION}
kind: VllmOptimizationPolicy
metadata:
  name: {name}
spec:
  target:
    apiVersion: apps/v1
    kind: Deployment
    namespace: {namespace}
    name: {workload}
    container: vllm
  objective:
    throughput:
      weight: 0
    p95TTFT:
      weight: 50
    p95InterTokenLatency:
      weight: 50
    minScoreGain: 1000000
  constraints:
    maxP95TTFT: 30s
    maxP95InterTokenLatency: 30s
  evaluation:
    baselineWindow: 30s
    trialWindow: 2m
    cooldown: 15s
    minFinishedRequests: 5
  adaptation:
    monitorInterval: 10s
    stableFor: 20s
    rescanInterval: 1h
    trafficChangeThresholdPercent: 20
  trafficComparison:
    tolerancePercent: 10
    minBackloggedFraction: 0.8
  parameters:
    maxNumBatchedTokens:
      min: 4096
      max: 5120
      step: 1024
"""


def _wait_for_policy_deletion(custom, name: str, timeout: int = 600) -> None:
    def gone() -> bool:
        try:
            get_crd(custom, "vllmoptimizationpolicies", name)
            return False
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise

    wait_for(gone, timeout=timeout, interval=5, message=f"policy {name} finalizer cleanup")


def _no_owned_child_policy(custom, policy_uid: str) -> bool:
    children = custom.list_cluster_custom_object(GROUP, VERSION, "containerargspolicies").get(
        "items", []
    )
    return not any(
        owner.get("uid") == policy_uid
        for child in children
        for owner in child.get("metadata", {}).get("ownerReferences", [])
    )


@pytest.mark.usefixtures("kind_cluster")
class TestVllmOptimization:
    @pytest.mark.timeout(7200)
    def test_pinned_vllm_metrics_adapt_rollback_and_delete(self, kube_context, k8s_clients):
        if kube_context.startswith("kind-"):
            pytest.skip(
                "vLLM optimization requires a real GPU cluster, not Kind's fake GPU capacity"
            )
        if not _gpu_node_available(k8s_clients.core):
            pytest.skip("cluster has no allocatable nvidia.com/gpu")

        try:
            k8s_clients.core.read_namespaced_service("prometheus", "monitoring")
        except ApiException as exc:
            if exc.status == 404:
                pytest.fail(
                    "install the e2e Prometheus scrape fixture first: "
                    "kubectl apply -k test/e2e/features/gpu/bootstrap/overlays"
                )
            raise
        wait_for_pod_ready(k8s_clients.core, "monitoring", "app=prometheus", timeout=300)

        suffix = uuid4().hex[:8]
        namespace = f"vllm-e2e-{suffix}"
        workload = f"vllm-server-{suffix}"
        policy_name = f"vllm-policy-{suffix}"
        created_policy = False
        policy_uid = ""
        gc = get_crd(k8s_clients.custom, "globalconfigurations", "global-config")
        original_prometheus_url = gc.get("spec", {}).get("prometheus", {}).get("url")
        prometheus_url_changed = False
        namespace_created = False
        try:
            k8s_clients.custom.patch_cluster_custom_object(
                GROUP,
                VERSION,
                "globalconfigurations",
                "global-config",
                {"spec": {"prometheus": {"url": PROMETHEUS_URL}}},
            )
            prometheus_url_changed = True
            k8s_clients.core.create_namespace({"metadata": {"name": namespace}})
            namespace_created = True
            _reload_prometheus(kube_context)
            apply_manifest(_deployment_manifest(namespace, workload), kube_context)
            wait_for_pod_ready(k8s_clients.core, namespace, f"app={workload}", timeout=1800)

            with _load_driver(kube_context, namespace, workload) as load:
                # Check the actual pinned image families and that high concurrency creates a queue.
                latest_values = {}

                def metrics_are_live() -> bool:
                    nonlocal latest_values
                    latest_values = _metric_values(kube_context, namespace, workload)
                    expected = set(COUNTER_FAMILIES + HISTOGRAM_COUNT_FAMILIES + GAUGE_FAMILIES)
                    return (
                        expected.issubset(latest_values)
                        and all(
                            latest_values[name] > 0
                            for name in COUNTER_FAMILIES + HISTOGRAM_COUNT_FAMILIES
                        )
                        and latest_values["vllm:num_requests_running"] > 0
                        and latest_values["vllm:num_requests_waiting"] > 0
                    )

                try:
                    wait_for(
                        metrics_are_live,
                        timeout=240,
                        interval=10,
                        message="pinned vLLM metric families under load",
                    )
                except TimeoutError as exc:
                    raise AssertionError(
                        f"{exc}; latest inference request error: {load.last_error}"
                    ) from exc
                short_prompt_mean = _prompt_mean(kube_context, namespace, workload)
                assert short_prompt_mean is not None and short_prompt_mean > 0

                apply_manifest(_policy_manifest(policy_name, namespace, workload), kube_context)
                created_policy = True
                policy_uid = get_crd(k8s_clients.custom, "vllmoptimizationpolicies", policy_name)[
                    "metadata"
                ]["uid"]

                settled = _wait_for_policy(
                    k8s_clients.custom,
                    policy_name,
                    lambda policy: (
                        policy.get("status", {}).get("phase") == "Monitoring"
                        and policy.get("status", {}).get("search", {}).get("completedAt")
                        and any(
                            condition.get("type") == "Ready" and condition.get("status") == "True"
                            for condition in policy.get("status", {}).get("conditions", [])
                        )
                    ),
                    timeout=2400,
                    message="first vLLM search to settle",
                )
                status = settled["status"]
                selected_value = _configuration_value(status.get("selectedConfiguration"))
                candidate_value = _configuration_value(
                    status.get("lastDecision", {}).get("candidateConfiguration")
                )
                assert selected_value == "4096", (
                    f"rejected trial changed selected value to {selected_value}"
                )
                assert candidate_value == "5120", f"unexpected first candidate {candidate_value}"
                assert _ready_pod_with_value(k8s_clients.core, namespace, workload, "4096")
                first_search_started = status["search"]["startedAt"]

                load.set_prompt_mix(32)
                prompt_mean = {"value": None}

                def prompt_mix_changed() -> bool:
                    prompt_mean["value"] = _prompt_mean(kube_context, namespace, workload)
                    return (
                        prompt_mean["value"] is not None
                        and prompt_mean["value"] > short_prompt_mean * 4
                    )

                wait_for(
                    prompt_mix_changed,
                    timeout=240,
                    interval=10,
                    message="prompt-token mean to reflect longer requests",
                )

                second_search = _wait_for_policy(
                    k8s_clients.custom,
                    policy_name,
                    lambda policy: (
                        policy.get("status", {}).get("search", {}).get("startedAt")
                        and policy["status"]["search"]["startedAt"] != first_search_started
                    ),
                    timeout=300,
                    message="second search after sustained prompt-mix change",
                )
                active = _wait_for_policy(
                    k8s_clients.custom,
                    policy_name,
                    lambda policy: (
                        policy.get("status", {}).get("phase") == "Tuning"
                        and policy.get("status", {}).get("plan", {}).get("phase") == "Evaluating"
                        and policy.get("status", {}).get("search", {}).get("startedAt")
                        == second_search["status"]["search"]["startedAt"]
                    ),
                    timeout=1200,
                    message="second vLLM candidate to reach evaluation",
                )
                second_candidate = _configuration_value(
                    active["status"]["plan"]["candidateConfiguration"]
                )
                assert second_candidate == "5120"
                wait_for(
                    lambda: _ready_pod_with_value(
                        k8s_clients.core, namespace, workload, second_candidate
                    ),
                    timeout=900,
                    interval=5,
                    message="second candidate pod readiness",
                )

                k8s_clients.custom.delete_cluster_custom_object(
                    GROUP, VERSION, "vllmoptimizationpolicies", policy_name
                )
                _wait_for_policy_deletion(k8s_clients.custom, policy_name)
                wait_for(
                    lambda: _ready_pod_with_value(k8s_clients.core, namespace, workload, "4096"),
                    timeout=1200,
                    interval=5,
                    message="original vLLM argument restoration after deletion",
                )
                wait_for(
                    lambda: _no_owned_child_policy(k8s_clients.custom, policy_uid),
                    timeout=180,
                    interval=5,
                    message="owned ContainerArgsPolicy deletion",
                )
                created_policy = False
        finally:
            try:
                if created_policy:
                    try:
                        k8s_clients.custom.delete_cluster_custom_object(
                            GROUP, VERSION, "vllmoptimizationpolicies", policy_name
                        )
                    except ApiException as exc:
                        if exc.status != 404:
                            raise
                    _wait_for_policy_deletion(k8s_clients.custom, policy_name)
                    created_policy = False
            finally:
                if prometheus_url_changed:
                    k8s_clients.custom.patch_cluster_custom_object(
                        GROUP,
                        VERSION,
                        "globalconfigurations",
                        "global-config",
                        {"spec": {"prometheus": {"url": original_prometheus_url}}},
                    )
            if namespace_created and not created_policy:
                try:
                    k8s_clients.core.delete_namespace(namespace)
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                wait_for(
                    lambda: namespace_gone(k8s_clients, namespace),
                    timeout=180,
                    message=f"namespace {namespace} removal",
                )
