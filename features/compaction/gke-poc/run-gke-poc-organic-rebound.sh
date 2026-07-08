#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GKE_POC_MODE="${GKE_POC_MODE:-organic-rebound}" \
  "${SCRIPT_DIR}/run-gke-poc.sh" "$@"
