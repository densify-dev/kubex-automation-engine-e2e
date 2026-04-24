"""Utilities for working with vendored example manifests in tests."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

import yaml

from helpers import kubectl, wait_for

EXAMPLES_ROOT = Path(
    os.environ.get("EXAMPLES_ROOT", Path(__file__).resolve().parent / "examples")
).resolve()
INVALID_EXAMPLES_ROOT = EXAMPLES_ROOT / "invalid"

OPTIONAL_API_GROUPS = {
    "autoscaling.k8s.io": [
        EXAMPLES_ROOT / "automationstrategy" / "vpa-filter.yaml",
        EXAMPLES_ROOT / "automationstrategy" / "vpa-filter-default.yaml",
        EXAMPLES_ROOT / "automationstrategy" / "vpa-filter-explicit-containers.yaml",
    ],
    "keda.sh": [
        EXAMPLES_ROOT / "staticpolicy" / "with-keda-hpa-filter.yaml",
    ],
}


def all_valid_example_manifests() -> list[Path]:
    return sorted(
        path
        for path in EXAMPLES_ROOT.rglob("*.yaml")
        if INVALID_EXAMPLES_ROOT not in path.parents
    )


def all_invalid_example_manifests() -> list[Path]:
    return sorted(INVALID_EXAMPLES_ROOT.rglob("*.yaml"))


def manifest_documents(manifest_path: Path) -> list[dict]:
    with manifest_path.open() as handle:
        return [
            doc
            for doc in yaml.safe_load_all(handle)
            if doc and doc.get("kind") and doc.get("metadata", {}).get("name")
        ]


@lru_cache(maxsize=None)
def api_group_available(kube_context: str, api_group: str) -> bool:
    output = kubectl(
        "api-resources",
        "--api-group",
        api_group,
        "-o",
        "name",
        context=kube_context,
    )
    return bool(output.strip())


def skip_reason(path: Path, kube_context: str) -> str | None:
    for api_group, manifests in OPTIONAL_API_GROUPS.items():
        if path in manifests and not api_group_available(kube_context, api_group):
            return f"optional API group {api_group} is not installed in the cluster"

    if path == EXAMPLES_ROOT / "automationstrategy" / "hpa-filter-container.yaml":
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                kube_context,
                "explain",
                "--api-version=autoscaling/v2",
                "horizontalpodautoscaler.spec.metrics.containerResource",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip()
            if detail:
                return (
                    "cluster does not support the HPA containerResource metric used by this example: "
                    f"{detail}"
                )
            return "cluster does not support the HPA containerResource metric used by this example"
    return None


def apply_manifest(manifest_path: Path, kube_context: str) -> None:
    kubectl("apply", "-f", str(manifest_path), context=kube_context)


def delete_manifest_in_reverse(manifest_path: Path, kube_context: str) -> None:
    """Delete manifest resources in reverse document order."""
    for doc in reversed(manifest_documents(manifest_path)):
        kind = doc["kind"]
        name = doc["metadata"]["name"]
        namespace = doc["metadata"].get("namespace")
        cmd = [
            "kubectl",
            "--context",
            kube_context,
            "delete",
            kind,
            name,
            "--ignore-not-found",
            "--wait=false",
        ]
        if namespace:
            cmd += ["-n", namespace]
        subprocess.run(cmd, capture_output=True)
        if kind == "Namespace":
            wait_for(
                lambda: not subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        kube_context,
                        "get",
                        "namespace",
                        name,
                    ],
                    capture_output=True,
                    text=True,
                ).returncode
                == 0,
                timeout=60,
                message=f"namespace {name} deletion",
            )


def assert_declared_resources_exist(manifest_path: Path, kube_context: str) -> None:
    for doc in manifest_documents(manifest_path):
        kind = doc["kind"]
        name = doc["metadata"]["name"]
        namespace = doc["metadata"].get("namespace")
        args = ["get", kind, name, "-o", "name"]
        if namespace:
            args += ["-n", namespace]
        kubectl(*args, context=kube_context)


def wait_for_declared_workloads_ready(manifest_path: Path, k8s_clients) -> None:
    for doc in manifest_documents(manifest_path):
        kind = doc["kind"]
        namespace = doc["metadata"].get("namespace")
        name = doc["metadata"]["name"]
        if kind != "Deployment":
            continue

        expected_replicas = doc.get("spec", {}).get("replicas", 1)

        wait_for(
            lambda: (
                (deployment := k8s_clients.apps.read_namespaced_deployment(name, namespace))
                and (deployment.status.ready_replicas or 0) >= expected_replicas
            ),
            timeout=180,
            message=f"deployment {namespace}/{name} readiness",
        )
