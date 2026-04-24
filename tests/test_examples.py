"""Tests: valid examples apply, invalid examples are rejected."""

import subprocess

import pytest

from example_utils import (
    EXAMPLES_ROOT,
    all_invalid_example_manifests,
    all_valid_example_manifests,
    apply_manifest,
    delete_manifest_in_reverse,
    skip_reason,
)


class TestExamples:
    """Validate vendored valid examples apply and invalid examples are rejected."""

    @pytest.mark.parametrize(
        "manifest_path",
        all_valid_example_manifests(),
        ids=lambda path: path.relative_to(EXAMPLES_ROOT).as_posix(),
    )
    def test_valid_example_manifest_apply_delete_round_trip(self, manifest_path, kube_context):
        reason = skip_reason(manifest_path, kube_context)
        if reason:
            pytest.skip(reason)

        try:
            apply_manifest(manifest_path, kube_context)
        finally:
            delete_manifest_in_reverse(manifest_path, kube_context)

    @pytest.mark.parametrize(
        "manifest_path",
        all_invalid_example_manifests(),
        ids=lambda path: path.relative_to(EXAMPLES_ROOT).as_posix(),
    )
    def test_invalid_example_manifest_rejected(self, manifest_path, kube_context):
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                kube_context,
                "apply",
                "--dry-run=server",
                "-f",
                str(manifest_path),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0, (
            "expected the intentionally invalid example bundle to be rejected"
        )
        assert (
            "bounds-invalid" in result.stderr
            or "cluster-bounds-invalid" in result.stderr
            or "strict decoding error" in result.stderr
        )
