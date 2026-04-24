import os
import subprocess
import sys
import time
from dataclasses import dataclass

import pytest
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from bootstrap import BootstrapConfig, bootstrap

# Make helpers.py importable from tests/ subdirectory
sys.path.insert(0, os.path.dirname(__file__))


def pytest_addoption(parser):
    parser.addoption("--kube-context", default="kind-e2e", help="kubectl context to use")
    parser.addoption(
        "--kind-cluster-name",
        default=None,
        help="Kind cluster name to create; defaults to kube context without the 'kind-' prefix",
    )
    parser.addoption(
        "--kind-node-image",
        default="kindest/node:v1.35.0",
        help="Kind node image to use when creating the cluster, for example kindest/node:v1.35.0",
    )
    parser.addoption(
        "--namespace", default="kubex", help="Namespace where the controller is deployed"
    )
    parser.addoption(
        "--helm-release",
        default="kubex-automation-engine",
        help="Helm release name used for controller installation",
    )
    parser.addoption(
        "--helm-repo-name", default="kubex", help="Helm repo name used for chart installation"
    )
    parser.addoption(
        "--helm-repo-url",
        default="https://densify-dev.github.io/helm-charts",
        help="Helm repo URL used for chart installation",
    )
    parser.addoption(
        "--helm-crds-chart",
        default="kubex/kubex-crds",
        help="Helm chart reference used for CRD installation",
    )
    parser.addoption(
        "--helm-controller-chart",
        default="kubex/kubex-automation-engine",
        help="Helm chart reference used for controller installation",
    )
    parser.addoption(
        "--helm-crds-chart-version",
        default=None,
        help="Helm chart version used for kubex-crds installations",
    )
    parser.addoption(
        "--helm-controller-chart-version",
        default=None,
        help="Helm chart version used for controller installations",
    )
    parser.addoption(
        "--controller-image-repository",
        default=None,
        help="Controller image repository used for Helm installation",
    )
    parser.addoption(
        "--controller-image-tag",
        default=None,
        help="Controller image tag used for Helm installation",
    )
    parser.addoption(
        "--controller-image-pull-policy",
        default="IfNotPresent",
        help="Controller image pull policy used for Helm installation",
    )
    parser.addoption(
        "--test-namespace", default="e2e-test", help="Namespace to create test workloads in"
    )
    parser.addoption(
        "--recommendations-file", default=None, help="Path to a recommendations JSON fixture file"
    )
    parser.addoption(
        "--keep-kind-cluster",
        action="store_true",
        help="Keep the Kind cluster after the test session",
    )
    parser.addoption(
        "--skip-kind-bootstrap",
        action="store_true",
        help="Skip Kind cluster bootstrap and use the existing kube context as-is",
    )
    parser.addoption(
        "--without-vpa", action="store_true", help="Do not install VPA into the test Kind cluster"
    )
    parser.addoption(
        "--without-keda", action="store_true", help="Do not install KEDA into the test Kind cluster"
    )
    parser.addoption(
        "--without-metrics-server",
        action="store_true",
        help="Do not install metrics-server into the test Kind cluster",
    )


@dataclass
class K8sClients:
    core: client.CoreV1Api
    apps: client.AppsV1Api
    custom: client.CustomObjectsApi
    rbac: client.RbacAuthorizationV1Api


@pytest.fixture(scope="session")
def kube_context(request):
    return request.config.getoption("--kube-context")


@pytest.fixture(scope="session")
def controller_namespace(request):
    return request.config.getoption("--namespace")


@pytest.fixture(scope="session")
def helm_release(request):
    return request.config.getoption("--helm-release")


@pytest.fixture(scope="session")
def helm_repo_name(request):
    return request.config.getoption("--helm-repo-name")


@pytest.fixture(scope="session")
def helm_repo_url(request):
    return request.config.getoption("--helm-repo-url")


@pytest.fixture(scope="session")
def helm_crds_chart(request):
    return request.config.getoption("--helm-crds-chart")


@pytest.fixture(scope="session")
def helm_controller_chart(request):
    return request.config.getoption("--helm-controller-chart")


@pytest.fixture(scope="session")
def helm_crds_chart_version(request):
    return request.config.getoption("--helm-crds-chart-version")


@pytest.fixture(scope="session")
def helm_controller_chart_version(request):
    return request.config.getoption("--helm-controller-chart-version")


@pytest.fixture(scope="session")
def controller_image_repository(request):
    return request.config.getoption("--controller-image-repository")


@pytest.fixture(scope="session")
def controller_image_tag(request):
    return request.config.getoption("--controller-image-tag")


@pytest.fixture(scope="session")
def controller_image_pull_policy(request):
    return request.config.getoption("--controller-image-pull-policy")


@pytest.fixture(scope="session")
def test_namespace(request):
    return request.config.getoption("--test-namespace")


@pytest.fixture(scope="session")
def recommendations_file(request):
    return request.config.getoption("--recommendations-file")


@pytest.fixture(scope="session")
def kind_cluster_name(request, kube_context):
    explicit_name = request.config.getoption("--kind-cluster-name")
    if explicit_name:
        return explicit_name
    if kube_context.startswith("kind-"):
        return kube_context.removeprefix("kind-")
    return kube_context


@pytest.fixture(scope="session")
def kind_cluster(
    request,
    kube_context,
    kind_cluster_name,
    helm_release,
    helm_repo_name,
    helm_repo_url,
    helm_crds_chart,
    helm_controller_chart,
    helm_crds_chart_version,
    helm_controller_chart_version,
    controller_image_repository,
    controller_image_tag,
    controller_image_pull_policy,
):
    if request.config.getoption("--skip-kind-bootstrap"):
        yield
        return

    bootstrap(
        BootstrapConfig(
            kube_context=kube_context,
            kind_cluster_name=kind_cluster_name,
            namespace=request.config.getoption("--namespace"),
            helm_release=helm_release,
            helm_repo_name=helm_repo_name,
            helm_repo_url=helm_repo_url,
            helm_crds_chart=helm_crds_chart,
            helm_controller_chart=helm_controller_chart,
            helm_crds_chart_version=helm_crds_chart_version,
            helm_controller_chart_version=helm_controller_chart_version,
            controller_image_repository=controller_image_repository,
            controller_image_tag=controller_image_tag,
            controller_image_pull_policy=controller_image_pull_policy,
            recommendations_file=request.config.getoption("--recommendations-file"),
            kind_node_image=request.config.getoption("--kind-node-image"),
            install_controller=True,
            install_metrics_server=not request.config.getoption("--without-metrics-server"),
            install_keda=not request.config.getoption("--without-keda"),
            install_vpa=not request.config.getoption("--without-vpa"),
        )
    )

    yield

    if request.config.getoption("--keep-kind-cluster"):
        return

    subprocess.run(
        ["kind", "delete", "cluster", "--name", kind_cluster_name],
        check=True,
    )


@pytest.fixture(scope="session")
def k8s_clients(kind_cluster, kube_context):
    """Load kubeconfig and return a bundle of Kubernetes API clients."""
    config.load_kube_config(context=kube_context)
    return K8sClients(
        core=client.CoreV1Api(),
        apps=client.AppsV1Api(),
        custom=client.CustomObjectsApi(),
        rbac=client.RbacAuthorizationV1Api(),
    )


@pytest.fixture(scope="session")
def kube_server_version(k8s_clients):
    # k8s_clients dependency ensures kubeconfig is loaded with the correct context
    return client.VersionApi().get_code()


@pytest.fixture(scope="session")
def supports_in_place_resize(kube_server_version):
    major = int(kube_server_version.major)
    minor = int(kube_server_version.minor.rstrip("+"))
    return (major, minor) >= (1, 35)


@pytest.fixture(scope="session")
def actual_in_place_resize_support(k8s_clients, test_namespace):
    pod_name = "e2e-in-place-resize-probe"
    try:
        k8s_clients.core.delete_namespaced_pod(pod_name, test_namespace)
    except ApiException:
        pass

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, namespace=test_namespace),
        spec=client.V1PodSpec(
            restart_policy="Always",
            containers=[
                client.V1Container(
                    name="app",
                    image="registry.k8s.io/pause:3.10",
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "100m", "memory": "128Mi"},
                        limits={"cpu": "100m", "memory": "128Mi"},
                    ),
                    resize_policy=[
                        client.V1ContainerResizePolicy(
                            resource_name="cpu", restart_policy="NotRequired"
                        ),
                        client.V1ContainerResizePolicy(
                            resource_name="memory", restart_policy="NotRequired"
                        ),
                    ],
                )
            ],
        ),
    )

    try:
        created = k8s_clients.core.create_namespaced_pod(test_namespace, pod)
        original_uid = created.metadata.uid

        deadline = time.time() + 120
        while time.time() < deadline:
            current = k8s_clients.core.read_namespaced_pod(pod_name, test_namespace)
            if (
                current.status.phase == "Running"
                and any(
                    condition.type == "Ready" and condition.status == "True"
                    for condition in (current.status.conditions or [])
                )
            ):
                break
            time.sleep(2)
        else:
            return False

        patch = {
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "resources": {
                            "requests": {"cpu": "250m", "memory": "192Mi"},
                            "limits": {"cpu": "250m", "memory": "192Mi"},
                        },
                    }
                ]
            }
        }
        k8s_clients.core.patch_namespaced_pod(pod_name, test_namespace, patch)

        deadline = time.time() + 120
        while time.time() < deadline:
            current = k8s_clients.core.read_namespaced_pod(pod_name, test_namespace)
            container = current.spec.containers[0]
            requests = container.resources.requests or {}
            limits = container.resources.limits or {}
            if (
                current.metadata.uid == original_uid
                and requests.get("cpu") == "250m"
                and requests.get("memory") == "192Mi"
                and limits.get("cpu") == "250m"
                and limits.get("memory") == "192Mi"
            ):
                return True
            time.sleep(2)
        return False
    except ApiException:
        return False
    finally:
        try:
            k8s_clients.core.delete_namespaced_pod(pod_name, test_namespace)
        except ApiException:
            pass


@pytest.fixture(scope="session", autouse=True)
def test_namespace_setup(k8s_clients, test_namespace):
    """Create the test namespace before the session and delete it after."""
    try:
        k8s_clients.core.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(name=test_namespace))
        )
    except ApiException as e:
        if e.status != 409:  # ignore AlreadyExists
            raise
    yield
    try:
        k8s_clients.core.delete_namespace(test_namespace)
    except ApiException:
        pass
