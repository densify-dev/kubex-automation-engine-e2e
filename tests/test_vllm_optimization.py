"""Experimental vLLM adaptation checks on a real single-GPU cluster."""

import json
import math
import os
import socket
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import yaml
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
# Native metadata is required from the artifact, not inferred from its identity.
NATIVE_IMAGE = "vllm/vllm-openai@sha256:8896fd7b29fc4ee7ce839f146cb4757984c476ec79d0234ffa8c995dee6e339a"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
HYBRID_MODEL = "google/gemma-2-2b-it"
HYBRID_MODEL_REVISION = "299a8560bedf22ed1c72a8a11e7dce4a7f9f51f8"
MODEL_NAME = "vllm-e2e"
PROMETHEUS_URL = "http://prometheus.monitoring.svc:9090"
BATCH_ARG = "--max-num-batched-tokens"
SEQ_ARG = "--max-num-seqs"

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
    "vllm:kv_cache_usage_perc",
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
        self.repetitions = 16
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
                    "max_tokens": 64,
                    "ignore_eos": True,
                    "temperature": 0,
                    "seed": 0,
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


def _require_native_capacity(kube_context: str, namespace: str, deployment: str) -> None:
    selector = f'namespace="{namespace}",pod=~"{deployment}-.*"'
    rows = prometheus_query(kube_context, "monitoring", f"vllm:cache_config_info{{{selector}}}")[
        "data"
    ]["result"]
    valid = bool(rows)
    for row in rows:
        metric = row["metric"]
        try:
            concurrency = float(metric.get("kv_cache_max_concurrency", "nan"))
            valid = valid and (
                metric.get("namespace") == namespace
                and bool(metric.get("pod"))
                and bool(metric.get("engine"))
                and math.isfinite(concurrency)
                and concurrency > 0
                and float(row["value"][1]) == 1
            )
        except (TypeError, ValueError):
            valid = False
    if not valid:
        pytest.fail(
            "NativeCapacity fixture blocked: pinned vLLM must expose scoped "
            "vllm:cache_config_info with engine and finite positive "
            "kv_cache_max_concurrency labels; no observed/block-count fallback. "
            "See Vllm-Optimization.md fixture prerequisites."
        )


@pytest.mark.parametrize("concurrency", ["9.9", None, "nan", "0", "invalid"])
def test_native_capacity_requires_metadata_capability(monkeypatch, concurrency):
    metric = {"namespace": "ns", "pod": "server-0", "engine": "0"}
    if concurrency is not None:
        metric["kv_cache_max_concurrency"] = concurrency
    monkeypatch.setitem(
        globals(),
        "prometheus_query",
        lambda *_: {"data": {"result": [{"metric": metric, "value": [0, "1"]}]}},
    )
    if concurrency == "9.9":
        _require_native_capacity("context", "ns", "server")
    else:
        with pytest.raises(pytest.fail.Exception, match="NativeCapacity fixture blocked"):
            _require_native_capacity("context", "ns", "server")


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


def _configuration_value(configuration: dict | None, name: str = BATCH_ARG) -> str | None:
    return _argument_value((configuration or {}).get("arguments", []), name)


def _assert_formula_evidence(policy: dict, sizing_basis: str) -> None:
    plan = policy["status"]["plan"]
    evidence = plan["formulaEvidence"]
    assert plan["stage"] == "Formula"
    assert plan["improvement"] == "maxNumSeqs"
    assert evidence["sizingBasis"] == sizing_basis
    assert evidence["batchLimit"] == 4096
    measurement = plan["baselineMeasurement"]
    assert evidence["windowEnd"] == measurement["observedAt"]
    window_start = datetime.fromisoformat(evidence["windowStart"].replace("Z", "+00:00"))
    window_end = datetime.fromisoformat(evidence["windowEnd"].replace("Z", "+00:00"))
    assert window_end - window_start == timedelta(seconds=30)
    measured_engines = {
        (pod["pod"], engine["engine"]): engine
        for pod in measurement["podMeasurements"]
        for engine in pod["engines"]
    }
    estimates = evidence["engineEstimates"]
    assert estimates
    assert {(e["pod"], e["engine"]) for e in estimates} == set(measured_engines)
    if sizing_basis == "ObservedCacheUsage":
        assert evidence["targetCacheUtilization"] == 0.8
    else:
        assert "targetCacheUtilization" not in evidence
    capacities = []
    for estimate in estimates:
        engine = measured_engines[(estimate["pod"], estimate["engine"])]
        if sizing_basis == "ObservedCacheUsage":
            running, usage = engine["meanRunningRequests"], engine["meanCacheUsage"]
            assert running > 0 and 0 < usage <= 1
            assert math.isclose(estimate["meanRunningRequests"], running, rel_tol=1e-6)
            assert math.isclose(estimate["meanCacheUsage"], usage, rel_tol=1e-6)
            capacity = math.floor(0.8 * running / usage)
        else:
            native = engine["nativeConcurrency"]
            assert math.isfinite(native) and native > 0
            assert estimate["nativeConcurrency"] == native
            capacity = math.floor(native)
        assert estimate["sequenceCapacity"] == capacity
        capacities.append(capacity)

    raw_estimate = min(capacities)
    target = min(max(raw_estimate, evidence["min"]), evidence["max"], 4096)
    assert evidence["rawEstimate"] == raw_estimate
    assert evidence["target"] == target
    assert _configuration_value(plan["candidateConfiguration"], SEQ_ARG) == str(target)
    assert _configuration_value(plan["candidateConfiguration"]) == "4096"


@pytest.mark.parametrize("sizing_basis", ["ObservedCacheUsage", "NativeCapacity"])
def test_formula_evidence_uses_per_engine_minimum(sizing_basis):
    engines = [
        {"engine": "0", "meanRunningRequests": 3, "meanCacheUsage": 0.25, "nativeConcurrency": 9.9},
        {"engine": "1", "meanRunningRequests": 2, "meanCacheUsage": 0.3, "nativeConcurrency": 5.9},
    ]
    estimates = [
        dict(engine, pod="pod", sequenceCapacity=9 if engine["engine"] == "0" else 5)
        for engine in engines
    ]
    evidence = {
        "sizingBasis": sizing_basis,
        "batchLimit": 4096,
        "min": 1,
        "max": 12,
        "windowStart": "2026-01-01T00:00:00Z",
        "windowEnd": "2026-01-01T00:00:30Z",
        "engineEstimates": estimates,
        "rawEstimate": 5,
        "target": 5,
    }
    if sizing_basis == "ObservedCacheUsage":
        evidence["targetCacheUtilization"] = 0.8
    policy = {
        "status": {
            "plan": {
                "stage": "Formula",
                "improvement": "maxNumSeqs",
                "formulaEvidence": evidence,
                "baselineMeasurement": {
                    "observedAt": evidence["windowEnd"],
                    "podMeasurements": [{"pod": "pod", "engines": engines}],
                },
                "candidateConfiguration": {
                    "arguments": [
                        {"name": SEQ_ARG, "value": "5"},
                        {"name": BATCH_ARG, "value": "4096"},
                    ]
                },
            }
        }
    }
    _assert_formula_evidence(policy, sizing_basis)
    evidence["rawEstimate"] = 9
    with pytest.raises(AssertionError):
        _assert_formula_evidence(policy, sizing_basis)


def _ready_pod_with_value(
    core, namespace: str, deployment: str, expected: str, sequences: str | None = None
) -> bool:
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

        def matches(name: str, value: str) -> bool:
            return any(
                arg == f"{name}={value}"
                or (arg == name and index + 1 < len(args) and args[index + 1] == value)
                for index, arg in enumerate(args)
            )

        if matches(BATCH_ARG, expected) and (sequences is None or matches(SEQ_ARG, sequences)):
            return True
    return False


def _gpu_node_available(core) -> bool:
    return any(
        int(node.status.allocatable.get("nvidia.com/gpu", "0")) > 0
        for node in core.list_node().items
    )


def _deployment_manifest(
    namespace: str, name: str, initial_sequences: str = "8", cache_manager: str = "full",
    image: str = IMAGE,
) -> str:
    model, revision = (
        (MODEL, MODEL_REVISION)
        if cache_manager == "full"
        else (HYBRID_MODEL, HYBRID_MODEL_REVISION)
    )
    token_env = ""
    if cache_manager == "hybrid":
        token_env = """\
          env:
            - name: HF_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hf-token
                  key: HF_TOKEN
"""
    cache_flag = (
        "--disable-hybrid-kv-cache-manager"
        if cache_manager == "full"
        else "--no-disable-hybrid-kv-cache-manager"
    )
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: 1
  progressDeadlineSeconds: 120
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
          image: {image}
          imagePullPolicy: IfNotPresent
          args:
            - --model
            - {model}
            - --revision
            - {revision}
            - --served-model-name
            - {MODEL_NAME}
            - --dtype
            - half
            - --max-model-len
            - "1024"
            - {BATCH_ARG}=4096
            - --max-num-seqs={initial_sequences}
            - --gpu-memory-utilization=0.9
            - --enable-chunked-prefill
            - --enable-prefix-caching
            - --seed=0
            - {cache_flag}
{token_env}          ports:
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


def _policy_manifest(name: str, namespace: str, workload: str, sizing_basis: str | None) -> str:
    formula_order = (
        "  formulaOrder:\n    - maxNumSeqs\n" if sizing_basis else "  formulaOrder: []\n"
    )
    sequence_settings = (
        f"    maxNumSeqs:\n      min: 1\n      max: 12\n      sizingBasis: {sizing_basis}\n"
        if sizing_basis
        else ""
    )
    if sizing_basis == "ObservedCacheUsage":
        sequence_settings += "      targetCacheUtilization: 0.8\n"
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
    p50TTFT:
      weight: 0
    gpuComputeUtilization:
      weight: 0
      direction: Maximize
    gpuMemoryFootprint:
      weight: 0
      direction: Minimize
    p95TTFT:
      weight: 50
    p95InterTokenLatency:
      weight: 50
    minScoreGain: 1000000
  constraints:
    maxP95TTFT: 10m
    maxP95InterTokenLatency: 10m
  evaluation:
    baselineWindow: 30s
    trialWindow: 2m
    cooldown: 15s
    minFinishedRequests: 5
    maxTrialsPerSearch: 12
  adaptation:
    monitorInterval: 10s
    stableFor: 20s
    rescanInterval: 1h
    trafficChangeThresholdPercent: 20
  trafficComparison:
    tolerancePercent: 10
    minBackloggedFraction: 0.8
{formula_order}  experimentOrder:
    - maxNumBatchedTokens
  parameters:
    maxNumBatchedTokens:
      min: 4096
      max: 5120
      step: 1024
{sequence_settings}"""


@pytest.mark.parametrize("cache_manager", ["full", "hybrid"])
@pytest.mark.parametrize("sizing_basis", ["ObservedCacheUsage", "NativeCapacity"])
def test_manifest_formula_modes_and_reproducible_flags(cache_manager, sizing_basis):
    deployment, _ = yaml.safe_load_all(_deployment_manifest("ns", "server", "32", cache_manager))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]
    expected_model, expected_revision = (
        ("Qwen/Qwen2.5-0.5B-Instruct", "7ae557604adf67be50417f59c2c2f167def9a775")
        if cache_manager == "full"
        else ("google/gemma-2-2b-it", "299a8560bedf22ed1c72a8a11e7dce4a7f9f51f8")
    )
    assert args[args.index("--model") + 1] == expected_model
    assert args[args.index("--revision") + 1] == expected_revision
    assert container.get("env", []) == (
        [
            {
                "name": "HF_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": "hf-token", "key": "HF_TOKEN"}},
            }
        ]
        if cache_manager == "hybrid"
        else []
    )
    flag = (
        "--disable-hybrid-kv-cache-manager"
        if cache_manager == "full"
        else "--no-disable-hybrid-kv-cache-manager"
    )
    assert flag in args
    assert {"--enable-prefix-caching", "--seed=0", "--enable-chunked-prefill"}.issubset(args)
    policy_manifest = _policy_manifest("policy", "ns", "server", sizing_basis)
    # Formula eligibility must not depend on fixture model/image identity.
    assert expected_model not in policy_manifest
    assert expected_revision not in policy_manifest
    assert IMAGE not in policy_manifest
    settings = yaml.safe_load(policy_manifest)["spec"]["parameters"]["maxNumSeqs"]
    assert set(settings) == (
        {"min", "max", "sizingBasis", "targetCacheUtilization"}
        if sizing_basis == "ObservedCacheUsage"
        else {"min", "max", "sizingBasis"}
    )
    assert settings["sizingBasis"] == sizing_basis
    assert ("targetCacheUtilization" in settings) == (sizing_basis == "ObservedCacheUsage")


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
    @pytest.mark.parametrize(
        "sizing_basis,failed_rollout",
        [(None, False), ("ObservedCacheUsage", False), ("NativeCapacity", False), (None, True)],
        ids=["batch-only", "observed-cache", "native-capacity", "failed-rollout"],
    )
    @pytest.mark.parametrize(
        "cache_manager",
        [
            "full",
            pytest.param(
                "hybrid",
                marks=pytest.mark.skipif(
                    not os.environ.get("HF_TOKEN"),
                    reason=(
                        "hybrid fixture requires HF_TOKEN with approved "
                        "gated Gemma2 model license access"
                    ),
                ),
            ),
        ],
    )
    def test_pinned_vllm_metrics_adapt_rollback_and_delete(
        self, kube_context, k8s_clients, sizing_basis, failed_rollout, cache_manager
    ):
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
        original_sequences = "32" if sizing_basis else "8"
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
            if cache_manager == "hybrid":
                k8s_clients.core.create_namespaced_secret(
                    namespace,
                    {
                        "metadata": {"name": "hf-token"},
                        "type": "Opaque",
                        "stringData": {"HF_TOKEN": os.environ["HF_TOKEN"]},
                    },
                )
            _reload_prometheus(kube_context)
            apply_manifest(
                _deployment_manifest(
                    namespace, workload, original_sequences, cache_manager,
                    NATIVE_IMAGE if sizing_basis == "NativeCapacity" else IMAGE,
                ),
                kube_context,
            )
            wait_for_pod_ready(k8s_clients.core, namespace, f"app={workload}", timeout=1800)

            with _load_driver(kube_context, namespace, workload) as load:
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
                        and 0 < latest_values["vllm:kv_cache_usage_perc"] <= 1
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
                if sizing_basis == "NativeCapacity":
                    _require_native_capacity(kube_context, namespace, workload)
                short_prompt_mean = _prompt_mean(kube_context, namespace, workload)
                assert short_prompt_mean is not None and short_prompt_mean > 0

                manifest = _policy_manifest(policy_name, namespace, workload, sizing_basis)
                if failed_rollout:
                    # vLLM rejects batch tokens below max-num-seqs, forcing startup failure.
                    manifest = manifest.replace(
                        "min: 4096\n      max: 5120\n      step: 1024",
                        "min: 1\n      max: 2\n      step: 1",
                    )
                apply_manifest(manifest, kube_context)
                created_policy = True
                policy_uid = get_crd(k8s_clients.custom, "vllmoptimizationpolicies", policy_name)[
                    "metadata"
                ]["uid"]

                if sizing_basis:
                    formula_trial = _wait_for_policy(
                        k8s_clients.custom,
                        policy_name,
                        lambda policy: (
                            policy.get("status", {}).get("search", {}).get("stage") == "Formula"
                            and policy.get("status", {}).get("plan", {}).get("stage") == "Formula"
                            and policy["status"]["plan"].get("phase") == "Evaluating"
                            and policy["status"]["plan"].get("improvement") == "maxNumSeqs"
                        ),
                        timeout=2400,
                        message="maxNumSeqs formula candidate evaluation",
                    )
                    _assert_formula_evidence(formula_trial, sizing_basis)
                    formula_target = _configuration_value(
                        formula_trial["status"]["plan"]["candidateConfiguration"], SEQ_ARG
                    )
                    wait_for(
                        lambda: _ready_pod_with_value(
                            k8s_clients.core, namespace, workload, "4096", formula_target
                        ),
                        timeout=900,
                        interval=5,
                        message="formula candidate pod readiness",
                    )
                    _wait_for_policy(
                        k8s_clients.custom,
                        policy_name,
                        lambda policy: (
                            policy.get("status", {}).get("plan", {}).get("stage") == "Formula"
                            and policy["status"]["plan"].get("phase") == "RollingBack"
                        ),
                        timeout=900,
                        message="formula candidate rollback",
                    )

                experiment_trial = _wait_for_policy(
                    k8s_clients.custom,
                    policy_name,
                    lambda policy: (
                        policy.get("status", {}).get("search", {}).get("stage") == "Experiment"
                        and policy.get("status", {}).get("plan", {}).get("stage") == "Experiment"
                        and policy["status"]["plan"].get("phase")
                        == ("RollingBack" if failed_rollout else "Evaluating")
                        and policy["status"]["plan"].get("improvement") == "maxNumBatchedTokens"
                    ),
                    timeout=2400,
                    message="maxNumBatchedTokens experiment evaluation",
                )
                experiment_configuration = experiment_trial["status"]["plan"][
                    "candidateConfiguration"
                ]
                assert _configuration_value(experiment_configuration) == (
                    "2" if failed_rollout else "5120"
                )
                if failed_rollout:
                    decision = experiment_trial["status"]["lastDecision"]
                    assert decision["outcome"] == "Failed"
                    assert "RolloutFailed" in decision["reason"]
                    wait_for(
                        lambda: _ready_pod_with_value(
                            k8s_clients.core, namespace, workload, "4096", original_sequences
                        ),
                        timeout=1200,
                        interval=5,
                        message="failed rollout restores prior arguments",
                    )
                    k8s_clients.custom.delete_cluster_custom_object(
                        GROUP, VERSION, "vllmoptimizationpolicies", policy_name
                    )
                    _wait_for_policy_deletion(k8s_clients.custom, policy_name)
                    wait_for(
                        lambda: _no_owned_child_policy(k8s_clients.custom, policy_uid),
                        timeout=180,
                        interval=5,
                        message="failed rollout child-policy cleanup",
                    )
                    created_policy = False
                    return
                if sizing_basis:
                    assert (
                        _configuration_value(experiment_configuration, SEQ_ARG)
                        == original_sequences
                    )
                wait_for(
                    lambda: _ready_pod_with_value(
                        k8s_clients.core, namespace, workload, "5120", original_sequences
                    ),
                    timeout=900,
                    interval=5,
                    message="batch experiment candidate pod readiness",
                )
                _wait_for_policy(
                    k8s_clients.custom,
                    policy_name,
                    lambda policy: (
                        policy.get("status", {}).get("plan", {}).get("stage") == "Experiment"
                        and policy["status"]["plan"].get("phase") == "RollingBack"
                    ),
                    timeout=900,
                    message="batch experiment rollback",
                )

                settled = _wait_for_policy(
                    k8s_clients.custom,
                    policy_name,
                    lambda policy: (
                        policy.get("status", {}).get("phase") == "Monitoring"
                        and policy.get("status", {}).get("search", {}).get("completedAt")
                    ),
                    timeout=2400,
                    message="first formula/experiment search to settle",
                )
                status = settled["status"]
                stages = [result["stage"] for result in status["search"].get("trialResults", [])]
                if sizing_basis:
                    assert stages[0] == "Formula"
                    assert all(stage == "Experiment" for stage in stages[1:])
                    assert status["search"]["formulaCursor"] == 1
                    formula_results = status["search"]["formulaResults"]
                    assert len(formula_results) == 1
                    assert formula_results[0]["improvement"] == "maxNumSeqs"
                    assert formula_results[0]["outcome"] in {"Rejected", "Deferred", "Failed"}
                else:
                    assert stages and all(stage == "Experiment" for stage in stages)
                assert _configuration_value(status.get("selectedConfiguration")) == "4096"
                if sizing_basis:
                    assert (
                        _configuration_value(status.get("selectedConfiguration"), SEQ_ARG)
                        == original_sequences
                    )
                assert _ready_pod_with_value(
                    k8s_clients.core, namespace, workload, "4096", original_sequences
                )
                first_search_started = status["search"]["startedAt"]

                load.set_prompt_mix(96)
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
                        and policy["status"]["search"].get("trigger") == "TrafficChange"
                    ),
                    timeout=600,
                    message="traffic-triggered vLLM rescan",
                )
                next_stage = "Formula" if sizing_basis else "Experiment"
                active = _wait_for_policy(
                    k8s_clients.custom,
                    policy_name,
                    lambda policy: (
                        policy.get("status", {}).get("search", {}).get("stage") == next_stage
                        and policy.get("status", {}).get("plan", {}).get("stage") == next_stage
                        and policy["status"]["plan"].get("phase") == "Evaluating"
                        and policy["status"]["search"].get("startedAt")
                        == second_search["status"]["search"]["startedAt"]
                    ),
                    timeout=1800,
                    message=f"traffic rescan {next_stage.lower()} candidate evaluation",
                )
                candidate = active["status"]["plan"]["candidateConfiguration"]
                expected_batch = "4096" if sizing_basis else "5120"
                expected_sequences = (
                    _configuration_value(candidate, SEQ_ARG) if sizing_basis else original_sequences
                )
                assert _configuration_value(candidate) == expected_batch
                wait_for(
                    lambda: _ready_pod_with_value(
                        k8s_clients.core, namespace, workload, expected_batch, expected_sequences
                    ),
                    timeout=900,
                    interval=5,
                    message="traffic-rescan candidate pod readiness",
                )

                k8s_clients.custom.delete_cluster_custom_object(
                    GROUP, VERSION, "vllmoptimizationpolicies", policy_name
                )
                _wait_for_policy_deletion(k8s_clients.custom, policy_name)
                wait_for(
                    lambda: _ready_pod_with_value(
                        k8s_clients.core, namespace, workload, "4096", original_sequences
                    ),
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
                    wait_for(
                        lambda: (
                            _ready_pod_with_value(
                                k8s_clients.core, namespace, workload, "4096", original_sequences
                            )
                            and _no_owned_child_policy(k8s_clients.custom, policy_uid)
                        ),
                        timeout=1200,
                        interval=5,
                        message="failure-path argument restoration and child cleanup",
                    )
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
