"""End-to-end coverage for namespaced and cluster-scoped object patches."""

from kubernetes.client.rest import ApiException

from helpers import GROUP, VERSION, delete_custom_object, get_crd, wait_for

OBJECT_PATCHES = "objectpatches"
CLUSTER_OBJECT_PATCHES = "clusterobjectpatches"
APPLY_ANNOTATION = "automation.kubex.ai/apply-patch"


def _condition(obj: dict, condition_type: str) -> dict:
    return next(
        condition
        for condition in obj.get("status", {}).get("conditions", [])
        if condition.get("type") == condition_type
    )


def _wait_applied(custom, plural: str, name: str, namespace: str | None = None) -> dict:
    def applied():
        obj = get_crd(custom, plural, name, namespace)
        ready = _condition(obj, "Ready")
        return ready.get("status") == "True" and ready.get("reason") == "Applied"

    wait_for(applied, timeout=120, message=f"{plural}/{name} applied")
    return get_crd(custom, plural, name, namespace)


class TestObjectPatch:
    """Verify RFC 7386 application and explicit reapplication triggers."""

    def test_object_patch_applies_and_preserves_unrelated_fields(self, k8s_clients, test_namespace):
        name = "e2e-object-patch"
        target_name = "e2e-object-patch-target"
        try:
            k8s_clients.core.create_namespaced_config_map(
                test_namespace,
                {
                    "metadata": {
                        "name": target_name,
                        "labels": {"keep": "label"},
                        "annotations": {"keep": "annotation", "remove": "yes"},
                    },
                    "data": {"keep": "original", "remove": "obsolete"},
                },
            )
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                test_namespace,
                OBJECT_PATCHES,
                {
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": "ObjectPatch",
                    "metadata": {"name": name},
                    "spec": {
                        "targetRef": {"apiVersion": "v1", "kind": "ConfigMap", "name": target_name},
                        "patch": {
                            "metadata": {
                                "labels": {"added": "label"},
                                "annotations": {"added": "annotation", "remove": None},
                            },
                            "data": {"added": "value", "remove": None},
                        },
                    },
                },
            )
            patch = _wait_applied(k8s_clients.custom, OBJECT_PATCHES, name, test_namespace)
            target = k8s_clients.core.read_namespaced_config_map(target_name, test_namespace)
            assert target.data == {"keep": "original", "added": "value"}
            assert target.metadata.labels == {"keep": "label", "added": "label"}
            assert target.metadata.annotations == {"keep": "annotation", "added": "annotation"}
            assert patch["spec"]["retryAmount"] == 3
            assert patch["status"]["state"] == "Applied"
            assert patch["status"]["observedGeneration"] == patch["metadata"]["generation"]
            assert patch["status"]["appliedGeneration"] == patch["metadata"]["generation"]
            assert _condition(patch, "Drifted")["status"] == "False"
        finally:
            delete_custom_object(
                k8s_clients.custom, GROUP, VERSION, test_namespace, OBJECT_PATCHES, name
            )
            try:
                k8s_clients.core.delete_namespaced_config_map(target_name, test_namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise

    def test_object_patch_spec_update_and_annotation_reapply(self, k8s_clients, test_namespace):
        name = "e2e-object-patch-reapply"
        target_name = "e2e-object-patch-reapply-target"
        try:
            k8s_clients.core.create_namespaced_config_map(
                test_namespace, {"metadata": {"name": target_name}, "data": {"value": "one"}}
            )
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                test_namespace,
                OBJECT_PATCHES,
                {
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": "ObjectPatch",
                    "metadata": {"name": name},
                    "spec": {
                        "targetRef": {"apiVersion": "v1", "kind": "ConfigMap", "name": target_name},
                        "patch": {"data": {"value": "two"}},
                    },
                },
            )
            patch = _wait_applied(k8s_clients.custom, OBJECT_PATCHES, name, test_namespace)
            generation = patch["metadata"]["generation"]
            k8s_clients.custom.patch_namespaced_custom_object(
                GROUP,
                VERSION,
                test_namespace,
                OBJECT_PATCHES,
                name,
                {"spec": {"patch": {"data": {"value": "three"}}}},
            )
            wait_for(
                lambda: (
                    k8s_clients.core.read_namespaced_config_map(target_name, test_namespace).data[
                        "value"
                    ]
                    == "three"
                ),
                timeout=120,
                message="ObjectPatch spec update",
            )
            patch = _wait_applied(k8s_clients.custom, OBJECT_PATCHES, name, test_namespace)
            assert patch["metadata"]["generation"] > generation

            k8s_clients.core.patch_namespaced_config_map(
                target_name, test_namespace, {"data": {"value": "external"}}
            )
            k8s_clients.custom.patch_namespaced_custom_object(
                GROUP,
                VERSION,
                test_namespace,
                OBJECT_PATCHES,
                name,
                {"metadata": {"annotations": {APPLY_ANNOTATION: "true"}}},
            )
            wait_for(
                lambda: (
                    k8s_clients.core.read_namespaced_config_map(target_name, test_namespace).data[
                        "value"
                    ]
                    == "three"
                ),
                timeout=120,
                message="ObjectPatch annotation reapply",
            )
            patch = _wait_applied(k8s_clients.custom, OBJECT_PATCHES, name, test_namespace)
            assert APPLY_ANNOTATION not in patch.get("metadata", {}).get("annotations", {})
        finally:
            delete_custom_object(
                k8s_clients.custom, GROUP, VERSION, test_namespace, OBJECT_PATCHES, name
            )
            try:
                k8s_clients.core.delete_namespaced_config_map(target_name, test_namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise

    def test_selector_replace_is_atomic_and_repairs_drift(self, k8s_clients, test_namespace):
        name = "e2e-object-patch-selector"
        target_name = "e2e-object-patch-selector-target"
        try:
            k8s_clients.apps.create_namespaced_deployment(
                test_namespace,
                {
                    "metadata": {"name": target_name, "labels": {"app": target_name}},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"app": target_name}},
                        "template": {
                            "metadata": {"labels": {"app": target_name}},
                            "spec": {
                                "containers": [
                                    {"name": "app", "image": "busybox", "args": ["--old"]},
                                    {
                                        "name": "sidecar",
                                        "image": "busybox",
                                        "args": ["--keep"],
                                    },
                                ]
                            },
                        },
                    },
                },
            )
            k8s_clients.custom.create_namespaced_custom_object(
                GROUP,
                VERSION,
                test_namespace,
                OBJECT_PATCHES,
                {
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": "ObjectPatch",
                    "metadata": {"name": name},
                    "spec": {
                        "targetRef": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": target_name,
                        },
                        "requeueInterval": "1s",
                        "drift": {
                            "retryInterval": "1s",
                            "retryAmount": 3,
                            "stabilizationWindow": "2s",
                        },
                        "patch": {"metadata": {"labels": {"managed-by": "kubex"}}},
                        "jsonPatch": [
                            {
                                "op": "replace",
                                "path": "/spec/template/spec/containers/@/args",
                                "selector": 'item.name == "app"',
                                "value": ["--serve"],
                            }
                        ],
                    },
                },
            )
            _wait_applied(k8s_clients.custom, OBJECT_PATCHES, name, test_namespace)
            target = k8s_clients.apps.read_namespaced_deployment(target_name, test_namespace)
            assert target.metadata.labels["managed-by"] == "kubex"
            assert target.spec.template.spec.containers[0].args == ["--serve"]
            assert target.spec.template.spec.containers[1].args == ["--keep"]

            k8s_clients.apps.patch_namespaced_deployment(
                target_name,
                test_namespace,
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {"name": "app", "args": ["--drifted"]},
                                    {"name": "sidecar", "args": ["--concurrent"]},
                                ]
                            }
                        }
                    }
                },
            )

            def repaired_with_concurrent_change():
                deployment = k8s_clients.apps.read_namespaced_deployment(
                    target_name, test_namespace
                )
                containers = deployment.spec.template.spec.containers
                return containers[0].args == ["--serve"] and containers[1].args == [
                    "--concurrent"
                ]

            wait_for(repaired_with_concurrent_change, timeout=120, message="selector drift repair")

        finally:
            delete_custom_object(
                k8s_clients.custom, GROUP, VERSION, test_namespace, OBJECT_PATCHES, name
            )
            try:
                k8s_clients.apps.delete_namespaced_deployment(target_name, test_namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise


class TestClusterObjectPatch:
    """Verify a cluster-scoped patch can mutate a Namespace."""

    def test_cluster_object_patch_applies_to_namespace(self, k8s_clients, test_namespace):
        name = "e2e-cluster-object-patch"
        target_name = f"{test_namespace}-object-patch"
        try:
            k8s_clients.core.create_namespace(
                {"metadata": {"name": target_name, "labels": {"keep": "label"}}}
            )
            k8s_clients.custom.create_cluster_custom_object(
                GROUP,
                VERSION,
                CLUSTER_OBJECT_PATCHES,
                {
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": "ClusterObjectPatch",
                    "metadata": {"name": name},
                    "spec": {
                        "targetRef": {"apiVersion": "v1", "kind": "Namespace", "name": target_name},
                        "patch": {"metadata": {"labels": {"added": "label"}}},
                    },
                },
            )
            patch = _wait_applied(k8s_clients.custom, CLUSTER_OBJECT_PATCHES, name)
            namespace = k8s_clients.core.read_namespace(target_name)
            labels = namespace.metadata.labels or {}
            assert labels.get("keep") == "label"
            assert labels.get("added") == "label"
            assert patch["status"]["state"] == "Applied"
            assert _condition(patch, "Ready")["reason"] == "Applied"
        finally:
            try:
                k8s_clients.custom.delete_cluster_custom_object(
                    GROUP, VERSION, CLUSTER_OBJECT_PATCHES, name
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
            try:
                k8s_clients.core.delete_namespace(target_name)
            except ApiException as exc:
                if exc.status != 404:
                    raise
