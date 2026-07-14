# Compaction E2E Feature

This feature currently has two GKE proof-of-movement sub-tests:

- `gke-poc/run-gke-poc.sh`: single-pass movement from the light node to a busy node
- `gke-poc/run-gke-poc-organic-rebound.sh`: eviction from the light node followed by an organic rebound attempt using staged candidate eligibility and gated non-preempting blockers

Both use the same minimal GKE POC workloads and descheduler policy.
