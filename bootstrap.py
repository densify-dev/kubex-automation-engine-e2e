"""Kind and Helm bootstrap helpers for the E2E framework."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
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
    kind_node_image: str | None = None
    install_controller: bool = True
    install_metrics_server: bool = True
    install_keda: bool = True
    install_vpa: bool = True
    cluster_name_value: str | None = None
    kubex_username: str = "dummy"
    kubex_epassword: str = "dummy"
    recommendations_file: str | None = None
    load_kind_images: bool = False


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
    if not config.recommendations_file:
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


def install_metrics_server(config: BootstrapConfig) -> None:
    run(
        "helm",
        "repo",
        "add",
        "metrics-server",
        "https://kubernetes-sigs.github.io/metrics-server",
    )
    run("helm", "repo", "update")
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
    finally:
        values_path.unlink(missing_ok=True)


def install_keda(config: BootstrapConfig) -> None:
    run("helm", "repo", "add", "kedacore", "https://kedacore.github.io/charts")
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
    images = ["densify/kubex-automation-cleanup:0.1.2"]
    if config.controller_image_repository and config.controller_image_tag:
        images.append(f"{config.controller_image_repository}:{config.controller_image_tag}")

    for image in images:
        run("kind", "load", "docker-image", image, "--name", config.kind_cluster_name)


def _chart_is_local(chart_ref: str) -> bool:
    return Path(chart_ref).exists()


def _helm_install_args(chart: str, version: str | None) -> list[str]:
    args = [chart]
    if version and not _chart_is_local(chart):
        args.extend(["--version", version])
    return args


def _controller_values(config: BootstrapConfig) -> dict:
    cluster_name = config.cluster_name_value or config.kind_cluster_name
    if config.recommendations_file and not config.cluster_name_value:
        cluster_name = "local-cluster"

    values = {
        "createSecrets": True,
        "kubex": {
            "url": {"host": "localhost"},
            "clusterName": cluster_name,
        },
        "kubexCredentials": {
            "username": config.kubex_username,
            "epassword": config.kubex_epassword,
        },
        "webhook": {"certManager": {"enabled": False}},
        "defaultAutomationStrategy": {"enabled": False},
    }
    if config.recommendations_file:
        values["localRecommendations"] = {
            "enabled": True,
            "configMapName": "recommendations",
            "fileName": "recommendations.json",
        }
        values["globalConfiguration"] = {"suppressFetchRecommendations": True}
    if config.controller_image_repository or config.controller_image_tag:
        if not (config.controller_image_repository and config.controller_image_tag):
            raise RuntimeError("controller image repository and tag must be set together")
        values["image"] = {
            "repository": config.controller_image_repository,
            "tag": config.controller_image_tag,
            "pullPolicy": config.controller_image_pull_policy,
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
    if not (_chart_is_local(config.helm_crds_chart) and _chart_is_local(config.helm_controller_chart)):
        run("helm", "repo", "add", config.helm_repo_name, config.helm_repo_url)
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
        post_renderer = Path(__file__).resolve().parent / "helm_post_renderer.py"
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
                "--post-renderer",
                sys.executable,
                "--post-renderer-args",
                str(post_renderer),
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
    parser.add_argument("--recommendations-file")
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
        recommendations_file=args.recommendations_file,
        kind_node_image=args.kind_node_image,
        load_kind_images=args.load_kind_images,
        install_controller=not args.no_controller,
        install_metrics_server=not args.without_metrics_server,
        install_keda=not args.without_keda,
        install_vpa=not args.without_vpa,
    )


if __name__ == "__main__":
    bootstrap(parse_args())
