#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GKE_COMPACTION_MODE="${GKE_COMPACTION_MODE:-organic-rebound}" \
  "${SCRIPT_DIR}/run-gke-compaction.sh" "$@"
