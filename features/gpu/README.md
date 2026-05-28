# GPU E2E Feature

This suite installs a deterministic mock GPU exporter and a local Prometheus instance.

Run it on a GPU-capable host or a Kind setup that exposes the host `/proc` mount used by the exporter.

Run it with:

```bash
GPU_SUITE=true \
GPU_KIND_CONFIG=test/e2e/features/gpu/kind-config.yaml \
./test/e2e/scripts/run-gpu-suite.sh
```
