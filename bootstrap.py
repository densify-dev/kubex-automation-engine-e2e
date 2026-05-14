"""Kind and Helm bootstrap helpers for the E2E framework."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BootstrapConfig:
    kube_context: str
    kind_cluster_name: str
    namespace: str = "kubex"
    helm_release: str = "kubex-automation-engine"
    helm_repo_name: str = "kubex"
    helm_repo_url: str = "https://densify-dev.github.io/helm-charts"
    helm_crds_chart: str = "kubex/kubex-crds"
    helm_controller_chart: str = "kubex/kubex-automation-engine"
    helm_crds_chart_version: str | None = None
    helm_controller_chart_version: str | None = None
    controller_image_repository: str | None = None
    controller_image_tag: str | None = None
    controller_image_pull_policy: str = "IfNotPresent"
    cleanup_image_repository: str | None = None
    cleanup_image_tag: str | None = None
    cleanup_image_pull_policy: str = "IfNotPresent"
    kind_node_image: str | None = None
    install_controller: bool = True
    install_metrics_server: bool = True
    install_keda: bool = True
    install_vpa: bool = True
    cluster_name_value: str | None = None
    kubex_username: str = "dummy"
    kubex_epassword: str | None = None
    kubex_url_host: str | None = None
    kubex_url_scheme: str | None = None
    recommendations_file: str | None = None
    load_kind_images: bool = False
    deploy_kubex_stub: bool = False


def run(
    *args: str,
    input_text: str | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(args)}", flush=True)
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture_output else sys.stdout,
        stderr=subprocess.PIPE if capture_output else sys.stderr,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr if capture_output else ""
        raise RuntimeError(f"command failed: {' '.join(args)} (exit {result.returncode})\n{detail}")
    return result


def _discover_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "examples" / "recommendations.json").is_file():
            return candidate
    raise RuntimeError("unable to locate repo root for recommendations fixture")


def ensure_kind_cluster(config: BootstrapConfig) -> None:
    clusters = run("kind", "get", "clusters", capture_output=True).stdout.splitlines()
    if config.kind_cluster_name in clusters:
        return

    args = ["kind", "create", "cluster", "--name", config.kind_cluster_name]
    if config.kind_node_image:
        args += ["--image", config.kind_node_image]
    run(*args)


def ensure_namespace(config: BootstrapConfig) -> None:
    manifest = run(
        "kubectl",
        "--context",
        config.kube_context,
        "create",
        "namespace",
        config.namespace,
        "--dry-run=client",
        "-o",
        "yaml",
        capture_output=True,
    ).stdout
    run(
        "kubectl",
        "--context",
        config.kube_context,
        "apply",
        "-f",
        "-",
        input_text=manifest,
    )


def ensure_recommendations_configmap(config: BootstrapConfig) -> None:
    if not config.recommendations_file or config.deploy_kubex_stub:
        return

    ensure_namespace(config)
    file_path = Path(config.recommendations_file).resolve()
    if not file_path.is_file():
        raise RuntimeError(f"recommendations file not found: {file_path}")

    manifest = run(
        "kubectl",
        "--context",
        config.kube_context,
        "create",
        "configmap",
        "recommendations",
        "--namespace",
        config.namespace,
        f"--from-file=recommendations.json={file_path}",
        "--dry-run=client",
        "-o",
        "yaml",
        capture_output=True,
    ).stdout
    run(
        "kubectl",
        "--context",
        config.kube_context,
        "apply",
        "-f",
        "-",
        input_text=manifest,
    )


def ensure_kubex_stub(config: BootstrapConfig) -> None:
    if not config.deploy_kubex_stub:
        return

    ensure_namespace(config)
    server_path = Path(__file__).with_name("mock_kubex_server.py")
    if not server_path.is_file():
        raise RuntimeError(f"mock Kubex server not found: {server_path}")

    script_manifest = run(
        "kubectl",
        "--context",
        config.kube_context,
        "create",
        "configmap",
        "kubex-stub-server",
        "--namespace",
        config.namespace,
        f"--from-file=mock_kubex_server.py={server_path}",
        "--dry-run=client",
        "-o",
        "yaml",
        capture_output=True,
    ).stdout
    run(
        "kubectl",
        "--context",
        config.kube_context,
        "apply",
        "-f",
        "-",
        input_text=script_manifest,
    )

    fixture_source = config.recommendations_file or str(
        _discover_repo_root(Path(__file__).resolve().parent) / "examples" / "recommendations.json"
    )
    fixture_path = Path(fixture_source).resolve()
    if not fixture_path.is_file():
        raise RuntimeError(f"recommendations fixture not found: {fixture_path}")

    fixture_manifest = run(
        "kubectl",
        "--context",
        config.kube_context,
        "create",
        "configmap",
        "kubex-stub-fixtures",
        "--namespace",
        config.namespace,
        f"--from-file=recommendations.json={fixture_path}",
        "--dry-run=client",
        "-o",
        "yaml",
        capture_output=True,
    ).stdout
    run(
        "kubectl",
        "--context",
        config.kube_context,
        "apply",
        "-f",
        "-",
        input_text=fixture_manifest,
    )

    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "kubex-stub", "namespace": config.namespace},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "kubex-stub"}},
            "template": {
                "metadata": {"labels": {"app": "kubex-stub"}},
                "spec": {
                    "containers": [
                        {
                            "name": "mock",
                            "image": "python:3.12-alpine",
                            "command": ["python", "/app/mock_kubex_server.py"],
                            "ports": [{"containerPort": 8080, "name": "http"}],
                            "env": [
                                {
                                    "name": "KUBEX_RECOMMENDATIONS_FILE",
                                    "value": "/fixtures/recommendations.json",
                                }
                            ],
                            "volumeMounts": [
                                {"name": "server", "mountPath": "/app", "readOnly": True},
                                {"name": "fixtures", "mountPath": "/fixtures", "readOnly": True},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "server", "configMap": {"name": "kubex-stub-server"}},
                        {"name": "fixtures", "configMap": {"name": "kubex-stub-fixtures"}},
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "kubex-stub", "namespace": config.namespace},
        "spec": {
            "selector": {"app": "kubex-stub"},
            "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
        },
    }
    run(
        "kubectl",
        "--context",
        config.kube_context,
        "apply",
        "-f",
        "-",
        input_text=json.dumps(manifest),
    )
    run(
        "kubectl",
        "--context",
        config.kube_context,
        "apply",
        "-f",
        "-",
        input_text=json.dumps(service),
    )
    run(
        "kubectl",
        "--context",
        config.kube_context,
        "rollout",
        "status",
        "deployment/kubex-stub",
        "--namespace",
        config.namespace,
        "--timeout=180s",
    )


def install_metrics_server(config: BootstrapConfig) -> None:
    run(
        "helm",
        "repo",
        "add",
        "--force-update",
        "metrics-server",
        "https://kubernetes-sigs.github.io/metrics-server",
    )
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(
            {
                "args": [
                    "--kubelet-insecure-tls",
                    "--kubelet-preferred-address-types=InternalIP,Hostname",
                ]
            },
            handle,
        )
        handle.flush()
        values_path = Path(handle.name)
    try:
        for attempt in range(1, 4):
            try:
                run("helm", "repo", "update")
                run(
                    "helm",
                    "upgrade",
                    "--install",
                    "metrics-server",
                    "metrics-server/metrics-server",
                    "--kube-context",
                    config.kube_context,
                    "--namespace",
                    "kube-system",
                    "--create-namespace",
                    "--wait",
                    "-f",
                    str(values_path),
                )
                return
            except RuntimeError:
                if attempt == 3:
                    raise
                print(f"metrics-server install failed on attempt {attempt}; retrying", flush=True)
                time.sleep(10)
    finally:
        values_path.unlink(missing_ok=True)


def install_keda(config: BootstrapConfig) -> None:
    run("helm", "repo", "add", "--force-update", "kedacore", "https://kedacore.github.io/charts")
    run("helm", "repo", "update")
    run(
        "helm",
        "upgrade",
        "--install",
        "keda",
        "kedacore/keda",
        "--kube-context",
        config.kube_context,
        "--namespace",
        "keda",
        "--create-namespace",
        "--wait",
    )


def install_vpa(config: BootstrapConfig) -> None:
    base = "https://raw.githubusercontent.com/kubernetes/autoscaler/vertical-pod-autoscaler-1.2.1/vertical-pod-autoscaler/deploy"

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "tls.crt"
        key_path = Path(tmpdir) / "tls.key"
        openssl_config = Path(tmpdir) / "openssl.cnf"
        openssl_config.write_text(
            """[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = vpa-webhook.kube-system.svc

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = vpa-webhook
DNS.2 = vpa-webhook.kube-system
DNS.3 = vpa-webhook.kube-system.svc
DNS.4 = vpa-webhook.kube-system.svc.cluster.local
"""
        )
        run(
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-days",
            "3650",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-config",
            str(openssl_config),
        )
        secret_manifest = run(
            "kubectl",
            "--context",
            config.kube_context,
            "create",
            "secret",
            "tls",
            "vpa-tls-certs",
            "--namespace",
            "kube-system",
            f"--cert={cert_path}",
            f"--key={key_path}",
            "--dry-run=client",
            "-o",
            "yaml",
            capture_output=True,
        ).stdout
        run(
            "kubectl",
            "--context",
            config.kube_context,
            "apply",
            "-f",
            "-",
            input_text=secret_manifest,
        )
    for file_name in (
        "vpa-v1-crd-gen.yaml",
        "vpa-rbac.yaml",
        "recommender-deployment.yaml",
        "updater-deployment.yaml",
        "admission-controller-deployment.yaml",
    ):
        run(
            "kubectl",
            "--context",
            config.kube_context,
            "apply",
            "-f",
            f"{base}/{file_name}",
        )


def load_kind_images(config: BootstrapConfig) -> None:
    images = []
    if config.cleanup_image_repository and config.cleanup_image_tag:
        images.append(f"{config.cleanup_image_repository}:{config.cleanup_image_tag}")
    else:
        images.append("densify/kubex-automation-cleanup:0.1.2")
    if config.controller_image_repository and config.controller_image_tag:
        images.append(f"{config.controller_image_repository}:{config.controller_image_tag}")

    for image in images:
        try:
            run("kind", "load", "docker-image", image, "--name", config.kind_cluster_name)
        except RuntimeError as err:
            print(
                f"kind load failed for {image}; falling back to direct ctr import: {err}",
                flush=True,
            )
            _import_kind_image_via_ctr(config.kind_cluster_name, image)


def _import_kind_image_via_ctr(kind_cluster_name: str, image: str) -> None:
    nodes = run(
        "docker",
        "ps",
        "--filter",
        f"label=io.x-k8s.kind.cluster={kind_cluster_name}",
        "--format",
        "{{.Names}}",
        capture_output=True,
    ).stdout.splitlines()
    if not nodes:
        raise RuntimeError(f"no Kind nodes found for cluster {kind_cluster_name}")

    for node in nodes:
        print(
            "+ docker save "
            f"{image} | docker exec -i {node} ctr -n=k8s.io images import --all-platforms -",
            flush=True,
        )
        docker_save = subprocess.Popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        docker_exec = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                node,
                "ctr",
                "-n=k8s.io",
                "images",
                "import",
                "--all-platforms",
                "-",
            ],
            stdin=docker_save.stdout,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=False,
        )
        assert docker_save.stdout is not None
        docker_save.stdout.close()
        docker_exec_return = docker_exec.wait()
        _, docker_save_stderr = docker_save.communicate()
        if docker_save.returncode != 0:
            detail = docker_save_stderr.decode(errors="replace")
            raise RuntimeError(
                f"docker save failed for {image} (exit {docker_save.returncode})\n{detail}"
            )
        if docker_exec_return != 0:
            raise RuntimeError(
                f"ctr images import failed for {image} on node {node} (exit {docker_exec_return})"
            )


def _chart_is_local(chart_ref: str) -> bool:
    return Path(chart_ref).exists()


def _helm_install_args(chart: str, version: str | None) -> list[str]:
    args = [chart]
    if version and not _chart_is_local(chart):
        args.extend(["--version", version])
    return args


def _controller_values(config: BootstrapConfig) -> dict:
    cluster_name = config.cluster_name_value or config.kind_cluster_name
    if (
        config.recommendations_file
        and not config.cluster_name_value
        and not config.deploy_kubex_stub
    ):
        cluster_name = "local-cluster"

    kubex_url_host = config.kubex_url_host or "localhost"
    kubex_url_scheme = config.kubex_url_scheme or "https"
    if config.deploy_kubex_stub:
        kubex_url_host = f"kubex-stub.{config.namespace}.svc.cluster.local:8080"
        kubex_url_scheme = "http"

    kubex_epassword = config.kubex_epassword or os.environ.get("KUBEX_E2E_EPASSWORD")
    if not config.deploy_kubex_stub and not kubex_epassword:
        raise RuntimeError(
            "KUBEX_E2E_EPASSWORD must be set when deploy_kubex_stub is disabled"
        )

    values = {
        "createSecrets": True,
        "kubex": {
            "url": {"host": kubex_url_host, "scheme": kubex_url_scheme},
            "clusterName": cluster_name,
        },
        "kubexCredentials": {
            "username": config.kubex_username,
            "epassword": kubex_epassword or "",
        },
        "webhook": {"certManager": {"enabled": False}},
        "defaultAutomationStrategy": {"enabled": False},
        "globalConfiguration": {"recommendationReloadInterval": "1m"},
    }
    if config.recommendations_file and not config.deploy_kubex_stub:
        values["localRecommendations"] = {
            "enabled": True,
            "configMapName": "recommendations",
            "fileName": "recommendations.json",
        }
        values["globalConfiguration"]["suppressFetchRecommendations"] = True
    if config.controller_image_repository or config.controller_image_tag:
        if not (config.controller_image_repository and config.controller_image_tag):
            raise RuntimeError("controller image repository and tag must be set together")
        values["image"] = {
            "repository": config.controller_image_repository,
            "tag": config.controller_image_tag,
            "pullPolicy": config.controller_image_pull_policy,
        }
    if config.cleanup_image_repository or config.cleanup_image_tag:
        if not (config.cleanup_image_repository and config.cleanup_image_tag):
            raise RuntimeError("cleanup image repository and tag must be set together")
        values["cleanup"] = {
            "image": {
                "repository": config.cleanup_image_repository,
                "tag": config.cleanup_image_tag,
                "pullPolicy": config.cleanup_image_pull_policy,
            }
        }
    return values


@contextmanager
def controller_values_file(config: BootstrapConfig):
    values = _controller_values(config)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(values, handle)
        handle.flush()
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def install_controller(config: BootstrapConfig) -> None:
    if not (
        _chart_is_local(config.helm_crds_chart)
        and _chart_is_local(config.helm_controller_chart)
    ):
        run("helm", "repo", "add", "--force-update", config.helm_repo_name, config.helm_repo_url)
        run("helm", "repo", "update")
    ensure_namespace(config)
    ensure_recommendations_configmap(config)
    run(
        "helm",
        "upgrade",
        "--install",
        "kubex-crds",
        *_helm_install_args(config.helm_crds_chart, config.helm_crds_chart_version),
        "--kube-context",
        config.kube_context,
        "--namespace",
        config.namespace,
        "--create-namespace",
        "--wait",
    )
    with controller_values_file(config) as values_file:
        try:
            run(
                "helm",
                "upgrade",
                "--install",
                config.helm_release,
                *_helm_install_args(
                    config.helm_controller_chart,
                    config.helm_controller_chart_version,
                ),
                "--kube-context",
                config.kube_context,
                "--namespace",
                config.namespace,
                "--create-namespace",
                "-f",
                str(values_file),
                "--wait",
                "--timeout",
                "10m0s",
            )
        except RuntimeError:
            print("=== controller pod diagnostics ===", flush=True)
            run(
                "kubectl",
                "--context",
                config.kube_context,
                "get",
                "pods",
                "-n",
                config.namespace,
                "-o",
                "wide",
                check=False,
            )
            run(
                "kubectl",
                "--context",
                config.kube_context,
                "describe",
                "pods",
                "-n",
                config.namespace,
                check=False,
            )
            run(
                "kubectl",
                "--context",
                config.kube_context,
                "logs",
                "-n",
                config.namespace,
                "--selector",
                f"app.kubernetes.io/name={config.helm_release}",
                "--tail=100",
                "--all-containers",
                check=False,
            )
            raise




def bootstrap(config: BootstrapConfig) -> None:
    ensure_kind_cluster(config)
    if config.load_kind_images:
        load_kind_images(config)
    if config.install_metrics_server:
        install_metrics_server(config)
    if config.install_keda:
        install_keda(config)
    if config.install_vpa:
        install_vpa(config)
    if config.deploy_kubex_stub:
        ensure_kubex_stub(config)
    if config.install_controller:
        install_controller(config)


def parse_args() -> BootstrapConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kube-context", required=True)
    parser.add_argument("--kind-cluster-name", required=True)
    parser.add_argument("--namespace", default="kubex")
    parser.add_argument("--helm-release", default="kubex-automation-engine")
    parser.add_argument("--helm-repo-name", default="kubex")
    parser.add_argument("--helm-repo-url", default="https://densify-dev.github.io/helm-charts")
    parser.add_argument("--helm-crds-chart", default="kubex/kubex-crds")
    parser.add_argument("--helm-controller-chart", default="kubex/kubex-automation-engine")
    parser.add_argument("--helm-crds-chart-version")
    parser.add_argument("--helm-controller-chart-version")
    parser.add_argument("--controller-image-repository")
    parser.add_argument("--controller-image-tag")
    parser.add_argument("--controller-image-pull-policy", default="IfNotPresent")
    parser.add_argument("--cleanup-image-repository")
    parser.add_argument("--cleanup-image-tag")
    parser.add_argument("--cleanup-image-pull-policy", default="IfNotPresent")
    parser.add_argument("--kubex-url-host")
    parser.add_argument("--kubex-url-scheme")
    parser.add_argument("--recommendations-file")
    parser.add_argument("--deploy-kubex-stub", action="store_true")
    parser.add_argument("--kind-node-image", default="kindest/node:v1.35.0")
    parser.add_argument("--load-kind-images", action="store_true")
    parser.add_argument("--no-controller", action="store_true")
    parser.add_argument("--without-metrics-server", action="store_true")
    parser.add_argument("--without-keda", action="store_true")
    parser.add_argument("--without-vpa", action="store_true")
    args = parser.parse_args()
    return BootstrapConfig(
        kube_context=args.kube_context,
        kind_cluster_name=args.kind_cluster_name,
        namespace=args.namespace,
        helm_release=args.helm_release,
        helm_repo_name=args.helm_repo_name,
        helm_repo_url=args.helm_repo_url,
        helm_crds_chart=args.helm_crds_chart,
        helm_controller_chart=args.helm_controller_chart,
        helm_crds_chart_version=args.helm_crds_chart_version,
        helm_controller_chart_version=args.helm_controller_chart_version,
        controller_image_repository=args.controller_image_repository,
        controller_image_tag=args.controller_image_tag,
        controller_image_pull_policy=args.controller_image_pull_policy,
        cleanup_image_repository=args.cleanup_image_repository,
        cleanup_image_tag=args.cleanup_image_tag,
        cleanup_image_pull_policy=args.cleanup_image_pull_policy,
        kubex_epassword=os.environ.get("KUBEX_E2E_EPASSWORD"),
        kubex_url_host=args.kubex_url_host,
        kubex_url_scheme=args.kubex_url_scheme,
        recommendations_file=args.recommendations_file,
        kind_node_image=args.kind_node_image,
        load_kind_images=args.load_kind_images,
        deploy_kubex_stub=args.deploy_kubex_stub,
        install_controller=not args.no_controller,
        install_metrics_server=not args.without_metrics_server,
        install_keda=not args.without_keda,
        install_vpa=not args.without_vpa,
    )


if __name__ == "__main__":
    bootstrap(parse_args())
