#!/usr/bin/env python3
"""Helm post-renderer for E2E-specific manifest overrides."""

from __future__ import annotations

import sys

import yaml


def _filter_gateway_container(doc: dict) -> dict:
    if not isinstance(doc, dict):
        return doc
    if doc.get("kind") != "Deployment":
        return doc

    metadata = doc.get("metadata") or {}
    labels = metadata.get("labels") or {}
    if labels.get("app.kubernetes.io/name") != "kubex-automation-engine":
        return doc

    template_spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = template_spec.get("containers") or []
    template_spec["containers"] = [
        container for container in containers if container.get("name") != "automation-gateway"
    ]
    return doc


def main() -> None:
    docs = list(yaml.safe_load_all(sys.stdin.read()))
    docs = [_filter_gateway_container(doc) for doc in docs]
    yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
